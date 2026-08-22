"""``rb release`` の nonce 対応（issue #8）を検証する。

確定仕様（issue #8 コメント最終版）:

- ``rb status`` に各宣言の nonce 先頭 8 桁を表示する
- ``rb release --nonce <値>`` は資源 ID 不要。前方一致で一意なら消す、曖昧なら拒否。
  **既定では自分が所有する宣言だけに絞り込む**（nonce の一致そのものを所有の証明に
  しない）。他人の宣言は ``--nonce --force`` でしか消せない
- ``resource`` と ``--nonce`` を両方渡し、絞り込んだ宣言の資源と食い違えば拒否する
- ``rb release <資源>`` は自分の宣言が 2 件以上なら ``EXIT_USAGE`` で拒否し、
  何も消さず候補を nonce 付きで並べる。1 件なら従来どおり消える
- ``rb release <資源> --all`` は自分の宣言を全部消す（従来の挙動を明示形で残す）
- ``rb release <資源> --force`` は変更しない
- ``rb run`` の自動解放（``_release_after_run``）は ``_cmd_release`` を経由しない

本ツールは資源を調べないので、ここでも実機には一切触れない。掲示板は
すべて ``tmp_path`` 上に作る（実運用の掲示板を絶対に触らない）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from resource_broker import cli
from resource_broker.board import Board, RemovalResult, build_entry
from resource_broker.cli import EXIT_BROKEN, EXIT_BUSY, EXIT_OK, EXIT_USAGE, main
from resource_broker.naming import normalize

RESOURCE = normalize("GPU0")

#: 自分とは無関係な作業ディレクトリ。**自分の cwd の祖先にしない**——祖先だと
#: cwd フォールバックで「自分のもの」に化けてしまい、``--force`` が所有を無視して
#: いることを検証できなくなる（test_ownership.py と同じ理由）。
FOREIGN_CWD = os.path.join(os.sep, "works", "theirs")


def audit_events(tmp_path: Path) -> list[dict]:
    """その掲示板の監査ログを全部読む（`rb history` と同じソース）。"""
    board = Board(tmp_path)
    records: list[dict] = []
    for path in board.audit_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def run(tmp_path: Path, *args: str) -> int:
    """一時的な掲示板に対して CLI を実行する。"""
    return main(["--home", str(tmp_path), *args])


def claim(tmp_path: Path, resource: str, job: str, *extra: str) -> int:
    """必須項目を埋めた claim。``--observed`` と ``--eta`` は省略できない。"""
    return run(
        tmp_path,
        "claim",
        resource,
        "--job",
        job,
        "--observed",
        "調べた",
        "--eta",
        "30m",
        *extra,
    )


def plant_with_nonce(
    board: Board,
    resource_id: str,
    nonce: str,
    *,
    job: str = "ジョブ",
    cwd: str | None = None,
    session: str | None = None,
    session_id: str | None = None,
) -> None:
    """指定した nonce を持つ宣言を直接仕込む。

    ``build_entry`` は nonce を自分で生成する（呼び出し側からは渡せない）ので、
    生成後に ``holder["nonce"]`` を書き換えてから ``declare`` する。前方一致の
    曖昧さを再現するには、複数の宣言が同じ接頭辞を持つ状態を意図的に作る必要が
    あり、乱数の偶然の衝突には頼れない。

    ``cwd`` / ``session`` / ``session_id`` を渡せば**他セッションの宣言**を仕込める。
    省略すると既定でテストプロセス自身のものになり、「自分の宣言」を作りたい
    ケースで使える。
    """
    entry = build_entry(resource_id, job=job, cwd=cwd, session=session, session_id=session_id)
    entry.holder["nonce"] = nonce
    assert board.declare(entry)


# --- 2 件以上あるときは何も消さず拒否する ----------------------------------------


def test_release_refuses_when_two_own_declarations_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自分の宣言が 2 件あるとき、``rb release <資源>`` は何も消さず EXIT_USAGE。"""
    assert claim(tmp_path, "GPU0", "1 本目") == EXIT_OK
    assert claim(tmp_path, "GPU0", "2 本目", "--share") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0") == EXIT_USAGE

    err = capsys.readouterr().err
    assert "2 件" in err
    # **何も消えていないことを掲示板で確認する。** 出力の文言だけでは不十分——
    # 「言ってることと実際にやったこと」が食い違う退行を捕まえるにはこれが要る。
    remaining = Board(tmp_path).list_for(RESOURCE)
    assert len(remaining) == 2
    assert {e.job for e in remaining} == {"1 本目", "2 本目"}


def test_release_still_removes_a_single_own_declaration(tmp_path: Path) -> None:
    """1 件だけなら従来どおり消える（拒否が常時発動する退行を捕まえる）。"""
    assert claim(tmp_path, "GPU0", "1 本目") == EXIT_OK

    assert run(tmp_path, "release", "GPU0") == EXIT_OK

    assert Board(tmp_path).list_for(RESOURCE) == []


# --- 消す前に止める: 数えてから消すまでの TOCTOU（issue #15 #3） ------------------


def test_release_own_does_not_sweep_a_declaration_that_appears_after_counting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_release_own`` は「1 件」と数えた宣言だけを消す。数えた後に現れた宣言は消さない。

    以前の実装は、1 件と数えたあとに ``nonce`` を固定せず ``remove_own`` を無条件
    （nonce 無し）で呼んでいた。``remove_own`` は自分の宣言をそのとき読み直して
    **全部**消すので、数えてから呼ぶまでの間に自分の 2 件目が現れると、両方とも
    消えていた——「消す前に止める」がまさに防ごうとした事故が競合下でそのまま
    成立していた。``_release_own`` は選択に ``Board.pairs_for_detailed`` を使う
    ので、それをフックして数え終えた直後に割り込ませる。
    """
    assert claim(tmp_path, "GPU0", "先に数えられる方") == EXIT_OK

    board = Board(tmp_path)
    original = Board.pairs_for_detailed
    injected = {"done": False}

    def _with_race(self: Board, resource_id: str):  # type: ignore[no-untyped-def]
        result = original(self, resource_id)
        # **数え終えた直後、1 回だけ割り込ませる。** ここでもう 1 件、自分の宣言を
        # 作る——`_release_own` が「1 件」と判定した直後の状態を再現する。
        if not injected["done"] and resource_id == RESOURCE:
            injected["done"] = True
            assert claim(tmp_path, "GPU0", "数えたあとに現れた方", "--share") == EXIT_OK
        return result

    monkeypatch.setattr(Board, "pairs_for_detailed", _with_race)

    assert run(tmp_path, "release", "GPU0") == EXIT_OK

    # **割り込ませた宣言は残っていなければならない。** 数えた対象（1 件目）だけが
    # 消え、数えた時点に存在しなかった 2 件目には触れていないことを確かめる。
    remaining = board.list_for(RESOURCE)
    assert [e.job for e in remaining] == ["数えたあとに現れた方"]


def test_release_own_does_not_proceed_when_the_count_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自分の宣言を「0 件」と数えたら、削除処理そのものへ進まない。

    以前の実装は 0 件のときも ``remove_own`` を無条件（nonce 無し）で呼んでいた。
    数えてから呼ぶまでの間に自分の宣言が新しく現れると、それを「数えた 1 件」と
    取り違えて消していた——数えた時点には存在しなかった宣言である。
    """
    board = Board(tmp_path)
    plant_with_nonce(
        board,
        RESOURCE,
        "f" * 32,
        job="他人の仕事",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )

    original = Board.pairs_for_detailed
    injected = {"done": False}

    def _with_race(self: Board, resource_id: str):  # type: ignore[no-untyped-def]
        result = original(self, resource_id)
        if not injected["done"] and resource_id == RESOURCE:
            injected["done"] = True
            # 「0 件」と数えた直後に、自分の宣言を割り込ませる。
            assert claim(tmp_path, "GPU0", "数えた後に現れた自分の宣言", "--share") == EXIT_OK
        return result

    monkeypatch.setattr(Board, "pairs_for_detailed", _with_race)

    assert run(tmp_path, "release", "GPU0") == EXIT_BUSY

    # **割り込ませた宣言も、元からあった他人の宣言も、両方残っている。**
    remaining = {e.job for e in board.list_for(RESOURCE)}
    assert remaining == {"他人の仕事", "数えた後に現れた自分の宣言"}


# --- 通常 release: 掲示板の一部が読めないときは断定しない（issue #17 指摘 3） --------


def test_release_own_refuses_when_the_whole_board_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """通常の ``rb release <資源>`` は、掲示板全体が読めなければ ``EXIT_BROKEN``。

    以前は ``list_for``（読めなかったものを黙って飛ばす）で数えていたので、
    掲示板全体が読めなくても「0 件」と断定して ``宣言はありませんでした`` と
    ``EXIT_OK`` を返していた——読めなかった側に自分の宣言が隠れていたかも
    しれないのに、成功として報告していたことになる。
    """
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    code = run(tmp_path, "release", "GPU0")

    assert code == EXIT_BROKEN
    assert code not in (EXIT_OK, EXIT_BUSY, EXIT_USAGE)
    assert "未確認" in capsys.readouterr().err


def test_release_own_refuses_on_an_unrelated_corrupt_file(tmp_path: Path) -> None:
    """自分の宣言があっても、**無関係な壊れたファイルがあれば**消さずに拒否する。

    壊れたファイルは中身が読めないので、それが実は自分の 2 件目の宣言だった
    可能性を否定できない。所有者を数える前の段階で情報が失われている。
    """
    assert claim(tmp_path, "GPU0", "自分の宣言") == EXIT_OK
    (tmp_path / "board" / "壊れている.json").write_text("not json", encoding="utf-8")

    assert run(tmp_path, "release", "GPU0") == EXIT_BROKEN
    # **拒否したのなら、何も消えていない。**
    assert len(Board(tmp_path).list_for(RESOURCE)) == 1


# --- UNCONFIRMED は EXIT_BUSY に畳まない（issue #18 指摘 4） ---------------------


def test_release_own_returns_exit_broken_when_the_deletion_cannot_be_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**選択後、削除の直前に掲示板が読めなくなったら** ``EXIT_BUSY`` ではなく ``EXIT_BROKEN``。

    ``remove_confirmed`` が返す ``UNCONFIRMED``（＝消せたか再確認できなかった）を、
    以前は ``FAILED``（掲示板は読めた上で消せなかった）と畳んでいたため、
    CLI は一律 ``EXIT_BUSY`` を返していた——「使用中で消せなかった」と
    「確認そのものが取れていない」は終了コードの意味が違う（issue #18 指摘 4）。

    捕獲（``os.rename``）を ``FileNotFoundError`` にして ``ABSENT`` を作り、
    その直後の再確認（``pairs_for_detailed``）も読めなくすることで、
    「選択した時点では完全に読めていたが、削除の窓で読めなくなった」を再現する。
    """
    assert claim(tmp_path, "GPU0", "消せるはずの宣言") == EXIT_OK

    import os as os_module

    def rename_always_absent(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("既に無い（捕獲時に強制する）")

    def read_text_always_broken(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("共有違反（注入）")

    original_remove_confirmed = Board.remove_confirmed
    state = {"done": False}

    def interleaved_remove_confirmed(  # type: ignore[no-untyped-def]
        self: Board, selection: object, *, reason: str, force: bool = False
    ):
        if not state["done"]:
            state["done"] = True
            monkeypatch.setattr(os_module, "rename", rename_always_absent)
            monkeypatch.setattr(Path, "read_text", read_text_always_broken)
        return original_remove_confirmed(self, selection, reason=reason, force=force)

    monkeypatch.setattr(Board, "remove_confirmed", interleaved_remove_confirmed)
    capsys.readouterr()

    code = run(tmp_path, "release", "GPU0")

    assert code == EXIT_BROKEN, f"確認できていないのに {code} を返した"
    assert code != EXIT_BUSY, "確認できないことを「使用中」に畳んでいる"
    assert "確認できませんでした" in capsys.readouterr().err


def test_release_by_nonce_force_returns_exit_broken_when_the_deletion_cannot_be_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--nonce --force`` でも同じ——``UNCONFIRMED`` は ``EXIT_BROKEN``。

    ``--force`` の単発削除は ``remove_confirmed`` を直接呼ぶ経路であり、
    ``remove_own`` を経由する経路とは別に終了コードの配線を確認する必要がある
    （issue #18 指摘 4 は「全経路が同じ表を通っているか」を問うている）。
    """
    assert claim(tmp_path, "GPU0", "消せるはずの宣言") == EXIT_OK
    board = Board(tmp_path)
    prefix = board.list_for(RESOURCE)[0].nonce[:8]

    import os as os_module

    def rename_always_absent(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("既に無い（捕獲時に強制する）")

    def read_text_always_broken(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("共有違反（注入）")

    # **一致・所有判定が済んだあと（削除の直前）にだけ壊す。** ここより前で
    # `Path.read_text` を壊すと、`_release_by_nonce` 冒頭の
    # `board.list_all_detailed()` そのものが不完全になり、この関数が本来
    # 検査したい「削除直前の窓」ではなく別の（既存の）分岐を通ってしまう。
    original_remove_confirmed = Board.remove_confirmed

    def interleaved_remove_confirmed(  # type: ignore[no-untyped-def]
        self: Board, selection: object, *, reason: str, force: bool = False
    ):
        monkeypatch.setattr(os_module, "rename", rename_always_absent)
        monkeypatch.setattr(Path, "read_text", read_text_always_broken)
        return original_remove_confirmed(self, selection, reason=reason, force=force)

    monkeypatch.setattr(Board, "remove_confirmed", interleaved_remove_confirmed)
    capsys.readouterr()

    code = run(tmp_path, "release", "--nonce", prefix, "--force")

    assert code == EXIT_BROKEN, f"確認できていないのに {code} を返した"
    assert code != EXIT_BUSY
    assert "確認できませんでした" in capsys.readouterr().err


def test_release_all_refuses_when_the_whole_board_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all`` も同じ族である。読めない掲示板を「0 件」と断定しない。"""
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    code = run(tmp_path, "release", "GPU0", "--all")

    assert code == EXIT_BROKEN
    assert code not in (EXIT_OK, EXIT_BUSY, EXIT_USAGE)
    assert "未確認" in capsys.readouterr().err


def test_release_all_does_not_sweep_a_declaration_that_appears_after_selecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--all`` も選択に使った実体だけを消す。選択後に現れた宣言は巻き込まない。

    ``--all`` は ``take_all=True`` で ``nonce`` を固定できないため、``_release_own``
    は選択で得た ``(Path, Entry)`` の並びをそのまま ``remove_own`` へ渡す
    （``declared=``）。渡さずに内部で再列挙すると、選択後に現れた宣言まで
    「自分の宣言だから」と一緒に消してしまう。
    """
    assert claim(tmp_path, "GPU0", "選択される方") == EXIT_OK

    original = Board.pairs_for_detailed
    injected = {"done": False}

    def _with_race(self: Board, resource_id: str):  # type: ignore[no-untyped-def]
        result = original(self, resource_id)
        if not injected["done"] and resource_id == RESOURCE:
            injected["done"] = True
            assert claim(tmp_path, "GPU0", "選択後に現れた方", "--share") == EXIT_OK
        return result

    monkeypatch.setattr(Board, "pairs_for_detailed", _with_race)

    assert run(tmp_path, "release", "GPU0", "--all") == EXIT_OK

    remaining = Board(tmp_path).list_for(RESOURCE)
    assert [e.job for e in remaining] == ["選択後に現れた方"]


# --- --nonce: 資源 ID 不要で 1 本だけ消す -----------------------------------------


def test_release_by_nonce_removes_only_the_matched_declaration(tmp_path: Path) -> None:
    """``--nonce`` は資源 ID 無しで前方一致の 1 本だけを消す。"""
    assert claim(tmp_path, "GPU0", "残す方") == EXIT_OK
    assert claim(tmp_path, "GPU0", "消す方", "--share") == EXIT_OK

    board = Board(tmp_path)
    entries = {e.job: e for e in board.list_for(RESOURCE)}
    target_nonce = entries["消す方"].nonce
    assert len(target_nonce) >= 8

    assert run(tmp_path, "release", "--nonce", target_nonce[:8]) == EXIT_OK

    remaining = board.list_for(RESOURCE)
    assert [e.job for e in remaining] == ["残す方"]


def test_release_by_nonce_ambiguous_prefix_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """前方一致が複数に当たったら、消さずに拒否する。"""
    board = Board(tmp_path)
    plant_with_nonce(board, RESOURCE, "aaaaaaaa" + "1" * 24, job="片方")
    plant_with_nonce(board, RESOURCE, "aaaaaaaa" + "2" * 24, job="もう片方")

    assert run(tmp_path, "release", "--nonce", "aaaaaaaa") == EXIT_USAGE

    err = capsys.readouterr().err
    assert "2 件" in err
    remaining = board.list_for(RESOURCE)
    assert len(remaining) == 2


def test_release_by_nonce_with_no_match_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 件も当たらなければ、その旨を言って拒否する（何も消さない拒否の対称形）。"""
    assert claim(tmp_path, "GPU0", "無関係") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "release", "--nonce", "ffffffff") == EXIT_USAGE
    assert "見つかりません" in capsys.readouterr().err
    assert len(Board(tmp_path).list_for(RESOURCE)) == 1


# --- --nonce: 掲示板が読めないとき「見つからない」も「一意」も断定しない（issue #15 #5） --


def test_release_by_nonce_does_not_confirm_release_when_the_whole_board_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """掲示板全体が読めないとき、``--nonce`` は「見つからない」と断定しない。

    以前の実装は ``board.list_all()`` を使っていた。docstring どおり読めなかった
    ものを黙って飛ばすので、掲示板全体が読めなくても ``matches == []`` になり、
    利用者の入力ミスを意味する ``EXIT_USAGE`` を返していた——実際には読めなかった
    だけで、一致する宣言が本当に無いとは限らない。

    **終了コードは ``EXIT_BROKEN``（3）。** ``EXIT_OK`` にすると「解放は未確認」で
    0 を返すことになり、走らなかった操作を成功と報告する形になる——
    ``rb release --nonce X && 次の手順`` と書いた呼び出し側は、解放されていない
    のに次へ進む（cli.py 冒頭「終了コードで嘘をつかない」）。``EXIT_BUSY`` も
    使わない——「掲示板が正常に読めた上で使用中」ではなく、読めなかっただけである。
    """
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    code = run(tmp_path, "release", "--nonce", "aaaaaaaa")

    assert code == EXIT_BROKEN
    assert code not in (EXIT_OK, EXIT_BUSY)
    err = capsys.readouterr().err
    assert "未確認" in err
    assert "見つかりません" not in err


def test_release_by_nonce_does_not_treat_a_partially_readable_board_as_unique(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分的にしか読めないとき、見えている 1 件を「一意」と誤認して消さない。

    以前の実装は、部分的に読めた場合でも見えている件数だけで一意性を判定して
    いた。読めなかった側に同じ prefix を持つ宣言が隠れていれば、実際には曖昧
    かもしれないし、見えている 1 件が自分のものでなければ「見つからない」が
    正しいかもしれない——どちらも確定できないまま削除まで進んでいた。
    """
    assert claim(tmp_path, "GPU0", "見える宣言") == EXIT_OK

    board = Board(tmp_path)
    prefix = board.list_for(RESOURCE)[0].nonce[:8]

    # 読めないファイルをもう 1 つ仕込む（Windows の共有違反を模す）。
    (tmp_path / "board" / "読めない.json").write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "読めない.json":
            raise PermissionError("共有違反")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky)

    assert run(tmp_path, "release", "--nonce", prefix) == EXIT_BROKEN

    err = capsys.readouterr().err
    assert "未確認" in err
    # **何も消えていないことを掲示板で確認する。**
    assert len(board.list_for(RESOURCE)) == 1


def test_release_by_nonce_unconfirmed_release_is_audited(tmp_path: Path) -> None:
    """未確認で終えたことも監査ログに残す（拒否と対称）。"""
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    assert run(tmp_path, "release", "--nonce", "aaaaaaaa") == EXIT_BROKEN

    events = audit_events(tmp_path)
    unconfirmed = [r for r in events if r.get("event") == "release_nonce_unconfirmed"]
    assert unconfirmed, f"release_nonce_unconfirmed が監査ログに無い: {events}"


# --- --nonce: 空文字列は「未指定」であって「全件一致」ではない ---------------------


def test_release_by_nonce_blank_is_rejected_without_removing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空白だけの ``--nonce`` は何も消さず拒否する（issue #8 独立検証の blocker）。

    ``entry.nonce.startswith("")`` は全件に一致するので、弾かずに通すと
    「曖昧なときに暗黙で 1 件選ぶ」がバグとして復活する——issue #8 が
    「採らなかった案」として明示的に退けた挙動である。
    """
    assert claim(tmp_path, "GPU0", "唯一の宣言") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "release", "--nonce", "   ") == EXIT_USAGE

    err = capsys.readouterr().err
    assert "空です" in err
    assert "見つかりません" not in err  # 「見つからない」とは別の理由である
    # **何も消えていないことを掲示板で確認する。**
    assert len(Board(tmp_path).list_for(RESOURCE)) == 1


def test_release_by_nonce_truly_empty_string_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--nonce ""``（空文字列そのもの）も同じ経路で拒否する。

    ``if args.nonce:`` のような真偽値分岐だと、空文字列は偽なので
    `--nonce` を渡していない扱いに落ちて `resource` 必須のエラーに化ける
    （別の理由での拒否になり、対処を誤らせる）。``is not None`` で拾うこと。
    """
    assert claim(tmp_path, "GPU0", "唯一の宣言") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "release", "--nonce", "") == EXIT_USAGE

    err = capsys.readouterr().err
    assert "空です" in err
    assert len(Board(tmp_path).list_for(RESOURCE)) == 1


def test_release_by_nonce_blank_with_force_is_also_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--nonce "   " --force`` も同様に拒否する（他人の宣言でも消えない）。

    掲示板に 1 件しかない状態で ``--force`` を併用すると、空の前方一致が
    「曖昧」判定すら経ずに単独の候補を選んでしまう経路がありうる——``--force``
    は所有チェックを外すためのものであって、入力の妥当性チェックまでは
    外さない。
    """
    board = Board(tmp_path)
    plant_with_nonce(
        board,
        RESOURCE,
        "f" * 32,
        job="他人のジョブ",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )

    assert run(tmp_path, "release", "--nonce", "   ", "--force") == EXIT_USAGE

    err = capsys.readouterr().err
    assert "空です" in err
    assert len(board.list_for(RESOURCE)) == 1


def test_release_by_nonce_single_digit_prefix_still_works_when_unique(
    tmp_path: Path,
) -> None:
    """1 桁の prefix でも一意に決まれば従来どおり消える。

    「空を弾く」修正が「短い prefix を弾く」に化ける退行を捕まえる回帰である
    （空だけが「未指定」であって、短いことそのものは問題ではない）。
    """
    assert claim(tmp_path, "GPU0", "唯一の宣言") == EXIT_OK

    board = Board(tmp_path)
    nonce = board.list_for(RESOURCE)[0].nonce
    one_digit = nonce[:1]
    # **前提を自分で確かめる。** 1 桁が本当に一意でなければ、このテストは
    # 「拒否されて当然」を「消えた」と誤読しかねない。
    assert sum(1 for e in board.list_all() if e.nonce.startswith(one_digit)) == 1

    assert run(tmp_path, "release", "--nonce", one_digit) == EXIT_OK

    assert board.list_for(RESOURCE) == []


def test_release_by_nonce_rejection_is_audited(tmp_path: Path) -> None:
    """拒否したことと理由が監査ログに残る（``_release_own`` の ``release_ambiguous`` と対称）。"""
    assert claim(tmp_path, "GPU0", "唯一の宣言") == EXIT_OK

    assert run(tmp_path, "release", "--nonce", "   ") == EXIT_USAGE

    events = audit_events(tmp_path)
    rejected = [r for r in events if r.get("event") == "release_nonce_rejected"]
    assert rejected, f"release_nonce_rejected が監査ログに無い: {events}"
    assert rejected[-1].get("reason")


# --- --nonce: 一意性は所有で絞る前に見る（issue #15 #4） -------------------------


def test_release_by_nonce_refuses_when_prefix_matches_mine_and_a_foreign_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """prefix が「自分 1 件＋他人 1 件」に当たるとき、何も消さずに拒否する。

    以前の実装は、前方一致した候補を先に「自分が所有するものだけ」へ絞ってから
    一意性を判定していた。この構成では自分の 1 件だけが残って「一意」に見え、
    利用者が他人側を指して打っていても検討にすら上らないまま**自分の宣言が
    黙って消えて**いた——Codex が指摘した事故そのものである。

    「他人」は cwd の祖先フォールバックが効く経路（``session_id`` を空にして
    ``Board.owns`` の判定を cwd 比較へ落とす）で作る。既存のテストはすべて
    cwd 無関係・``session_id`` 双方非空の組み合わせだけを使っており、
    ``Board.owns`` がこの経路（cwd 比較）を通るケースは未検証だった。
    """
    board = Board(tmp_path)
    shared_prefix = "c0ffee12"
    plant_with_nonce(board, RESOURCE, shared_prefix + "1" * 24, job="自分の仕事")
    plant_with_nonce(
        board,
        RESOURCE,
        shared_prefix + "2" * 24,
        job="他人の仕事",
        cwd=FOREIGN_CWD,
        session="other-session",
        session_id="",  # 空にする——cwd 比較（祖先フォールバック）の経路を通す
    )

    assert run(tmp_path, "release", "--nonce", shared_prefix) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "2 件" in err
    # **何も消えていないことを掲示板で確認する。** 自分の宣言・他人の宣言のどちらも
    # 残っている——狙いすら決まらない「曖昧」な状態であって、片方だけが黙って
    # 消える状態ではない。
    remaining = board.list_for(RESOURCE)
    assert len(remaining) == 2
    assert {e.job for e in remaining} == {"自分の仕事", "他人の仕事"}


# --- --nonce: 所有を尊重する / --force が個体指定で他人を消す唯一の道 ------------


def test_release_by_nonce_refuses_someone_elses_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """他人の宣言にしか当たらなければ、消さずに ``--force`` を案内する。

    ``rb status`` が nonce の先頭 8 桁を全セッションへ見せるようになった以上、
    「nonce を知っている＝自分の宣言」という前提はもう成立しない。「見つからない」
    （0 件一致）とは区別する——対処がまるで違う（打ち直す／--force を検討する）。
    """
    board = Board(tmp_path)
    plant_with_nonce(
        board,
        RESOURCE,
        "d" * 32,
        job="他人のジョブ",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )

    assert run(tmp_path, "release", "--nonce", "d" * 8) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "自分の宣言ではありません" in err
    assert "--force" in err
    # **何も消えていないことを掲示板で確認する。**
    assert len(board.list_for(RESOURCE)) == 1


def test_release_by_nonce_with_force_removes_someone_elses_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--nonce --force`` は所有を問わず消す。個体指定で他人を消す唯一の道である。

    ``_release_forced`` と同じく、**何を消したかを必ず言う**——黙って他人の宣言を
    消すと、消された側は理由の分からない消失として体験する。
    """
    board = Board(tmp_path)
    plant_with_nonce(
        board,
        RESOURCE,
        "e" * 32,
        job="他人のジョブ",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )

    assert run(tmp_path, "release", "--nonce", "e" * 8, "--force") == EXIT_OK

    out = capsys.readouterr().out
    assert "theirs" in out
    assert "他人のジョブ" in out
    assert board.list_for(RESOURCE) == []


def test_release_by_nonce_rejects_when_resource_mismatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``resource`` と ``--nonce`` を両方渡し、絞り込んだ宣言の資源と食い違えば拒否する。

    黙って通すと、利用者が「GPU0 を消す」と信じて打ったのに無関係な資源
    （実際には COM3）の宣言を消してしまう——この変更が無くそうとした事故そのもの。
    """
    assert claim(tmp_path, "COM3", "実は COM3") == EXIT_OK

    board = Board(tmp_path)
    nonce = board.list_for(normalize("COM3"))[0].nonce

    assert run(tmp_path, "release", "GPU0", "--nonce", nonce[:8]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "食い違い" in err
    assert len(board.list_for(normalize("COM3"))) == 1


# --- --nonce: 削除呼び出しに cwd を転送する（独立検証の指摘 #2） ------------------


def test_release_by_nonce_forwards_cwd_to_the_final_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_release_by_nonce`` の最終削除は ``cwd`` を ``Board.remove_own`` へ転送する。

    通常の（nonce を持つ）宣言は ``Board.owns`` の nonce 一致だけで所有が決まるため
    ``cwd`` が無くても症状化しない。実害が出るのは **nonce を持たない旧形式の宣言**
    （cwd の祖先フォールバックでしか所有と判定できないもの）に限られる——それでも
    掲示板に残っているのに「既に無い可能性」と嘘を言わないための保険として、
    呼び出しそのものに ``cwd`` が乗っていることを直接確かめる（``_release_own``
    は既に渡しており、対称にする）。
    """
    assert claim(tmp_path, "GPU0", "確認対象") == EXIT_OK
    board = Board(tmp_path)
    nonce = board.list_for(RESOURCE)[0].nonce

    calls: list[dict] = []
    original_remove_own = Board.remove_own

    def _spy(self: Board, resource_id: str, **kwargs: object) -> object:
        calls.append(kwargs)
        return original_remove_own(self, resource_id, **kwargs)

    monkeypatch.setattr(Board, "remove_own", _spy)

    assert run(tmp_path, "release", "--nonce", nonce[:8]) == EXIT_OK

    assert calls, "Board.remove_own が呼ばれていない"
    assert calls[-1].get("cwd") is not None


# --- --nonce: 削除直前に対象が入れ替わっても新しい宣言を消さない（TOCTOU） --------


def test_release_by_nonce_does_not_remove_a_declaration_reclaimed_mid_flight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--nonce`` でも、選択後に対象が消えて別の宣言に入れ替わっていれば消さない。

    ``_release_by_nonce`` は選択で得た実体を :class:`ConfirmedEntry` にして
    ``remove_confirmed`` へ渡す。渡した実体そのものが既に無くても、**別の宣言が
    そこに現れていれば「無い」ではなく「入れ替わった」**と答えなければならない
    ——でなければ、消していない他人の生きた宣言を「解放した」と偽ることになる
    （issue #17 指摘 2・3）。
    """
    assert claim(tmp_path, "GPU0", "先に居た方") == EXIT_OK
    board = Board(tmp_path)
    prefix = board.list_for(RESOURCE)[0].nonce[:8]

    original = Board.remove_confirmed
    state = {"nested": False}

    def interleave(
        self: Board, selection: object, *, reason: str, force: bool = False
    ) -> RemovalResult:
        if not state["nested"]:
            state["nested"] = True
            # 選択が終わった直後、削除の直前に T が force で取り直す。
            assert run(tmp_path, "release", "GPU0", "--force") == EXIT_OK
            assert claim(tmp_path, "GPU0", "後から入れ替わった方") == EXIT_OK
        return original(self, selection, reason=reason, force=force)

    monkeypatch.setattr(Board, "remove_confirmed", interleave)
    capsys.readouterr()

    code = run(tmp_path, "release", "--nonce", prefix)

    assert code == EXIT_BUSY
    assert "入れ替わりました" in capsys.readouterr().err
    remaining = board.list_for(RESOURCE)
    assert [e.job for e in remaining] == ["後から入れ替わった方"]


# --- --all: 自分の宣言を全部消す -------------------------------------------------


def test_release_all_removes_every_own_declaration(tmp_path: Path) -> None:
    """``--all`` は自分の宣言を全部消す（従来の挙動を明示形で残す）。"""
    assert claim(tmp_path, "GPU0", "1 本目") == EXIT_OK
    assert claim(tmp_path, "GPU0", "2 本目", "--share") == EXIT_OK

    assert run(tmp_path, "release", "GPU0", "--all") == EXIT_OK

    assert Board(tmp_path).list_for(RESOURCE) == []


# --- --force: 挙動を変えない ------------------------------------------------------


def test_release_force_still_removes_everything_regardless_of_ownership(
    tmp_path: Path,
) -> None:
    """``--force`` は所有を問わず全部消す。変更していないことの回帰。

    2 本とも**自分のものではない**（cwd も session_id も無関係）宣言にする。
    そうでないと、``--all`` 相当（所有者フィルタありの全消し）へ壊れても
    たまたま同じ結果になり、``--force`` が所有を無視しているかどうかを
    検証できない。
    """
    board = Board(tmp_path)
    plant_with_nonce(
        board,
        RESOURCE,
        "b" * 32,
        job="他人その1",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )
    plant_with_nonce(
        board,
        RESOURCE,
        "c" * 32,
        job="他人その2",
        cwd=FOREIGN_CWD,
        session="theirs",
        session_id="theirs",
    )

    assert run(tmp_path, "release", "GPU0", "--force") == EXIT_OK

    assert board.list_for(RESOURCE) == []


def test_release_force_does_not_sweep_a_declaration_that_appears_after_selecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` も選択に使った実体だけを消す。選択後に現れた宣言は巻き込まない。

    以前は表示用に ``pairs_for_detailed`` で列挙したあと、削除は
    ``Board.remove_all`` が内部で ``pairs_for`` により**別に列挙し直して**いた
    ——選択と削除が別々の読み取りに基づくため、表示した対象と実際に消した
    対象が食い違いうる（issue #15 指摘 12・issue #18 末尾）。いまは
    :meth:`BoardListing.confirmed` で得た同じ並びをそのまま
    :meth:`Board.remove_selected` へ渡すので、選択後に現れた宣言（``--force``
    でも一度も見ていない個体）は対象に入らない。
    """
    assert claim(tmp_path, "GPU0", "選択される方") == EXIT_OK

    original = Board.pairs_for_detailed
    injected = {"done": False}

    def _with_race(self: Board, resource_id: str):  # type: ignore[no-untyped-def]
        result = original(self, resource_id)
        if not injected["done"] and resource_id == RESOURCE:
            injected["done"] = True
            assert claim(tmp_path, "GPU0", "選択後に現れた方", "--share") == EXIT_OK
        return result

    monkeypatch.setattr(Board, "pairs_for_detailed", _with_race)

    assert run(tmp_path, "release", "GPU0", "--force") == EXIT_OK

    remaining = Board(tmp_path).list_for(RESOURCE)
    assert [e.job for e in remaining] == ["選択後に現れた方"]


def test_board_has_no_resource_name_only_wipe_method() -> None:
    """**資源名だけで何件消えるか決まる公開入口は無い。**

    ``Board.remove_all(resource_id)`` のような「名前で一掃する」メソッドは
    廃した——削除の公開入口（:meth:`Board.remove_confirmed` /
    :meth:`Board.remove_own` / :meth:`Board.remove_selected`）は、いずれも
    掲示板を読んだ結果として列挙された個体（``ConfirmedEntry`` の並び）を
    引数として要求する。この構造そのものを、公開 API の型シグネチャから
    直接確かめる——「この関数は名前だけで何件消えるか決まるか」を問い、
    Yes になる公開メソッドが 1 つも無いことを列挙して固定する。
    """
    import inspect

    assert not hasattr(Board, "remove_all"), "資源名だけで全消しする入口が復活している"

    destructive = ("remove_confirmed", "remove_own", "remove_selected")
    for name in destructive:
        method = getattr(Board, name)
        params = inspect.signature(method).parameters
        # **「選択の並び」を表す引数を必ず持つ。** 名前は経路ごとに違うが、
        # いずれも「掲示板を読んだ結果」を渡さなければ呼べない形になっている
        # ことを、シグネチャの存在で確かめる。
        assert {"selection", "declared", "selections"} & set(params), (
            f"{name} が選択の並びを引数として要求していない: {list(params)}"
        )


def test_release_force_refuses_when_the_whole_board_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--force`` も同じ族である。掲示板全体が読めなければ ``EXIT_BROKEN``。

    「全部消す」と言っている以上、読めなかった側にこの資源の宣言が隠れたまま
    「強制解放しました」と言ってはならない（issue #17 指摘 2・4「終了コードの
    契約」）。
    """
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    code = run(tmp_path, "release", "GPU0", "--force")

    assert code == EXIT_BROKEN
    assert code not in (EXIT_OK, EXIT_BUSY, EXIT_USAGE)
    assert "未確認" in capsys.readouterr().err


def test_release_force_returns_exit_broken_when_some_removals_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` の削除が一部 I/O で失敗したら、``EXIT_OK`` ではなく ``EXIT_BROKEN``。

    以前は警告を出すだけで終了コードは ``EXIT_OK`` のままだった。
    ``release --force && 次の手順`` のように使われれば、消えていない宣言が
    残ったまま次へ進む——**終了コードで嘘をつかない**（cli.py 冒頭）。
    """
    assert claim(tmp_path, "GPU0", "1 本目") == EXIT_OK
    assert claim(tmp_path, "GPU0", "2 本目", "--share") == EXIT_OK

    import os as os_module

    from resource_broker.board import UNLINK_ATTEMPTS

    # **`os.rename`（CAS の「捕まえる」段）を失敗させる。** 削除を実際に塞ぐのは
    # ここであって、捕まえたあとの tombstone の後始末（`_unlink_with_retry`）では
    # ない——tombstone だけが消せなくても、掲示板からは既に見えなくなっている
    # ので「消せた」扱いになる（DESIGN.md「Known Residuals」）。
    original = os_module.rename
    calls = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> None:
        # **最初の 1 件だけ、やり直しの回数を使い切らせて失敗させる。** 1 回の
        # 失敗は `_rename_with_retry` が自分で吸収してしまうため、2 件のうち
        # 1 件だけが消えない状況（恒常的な共有違反）を作るには、その 1 件の
        # 再試行を全部潰す必要がある。以降（2 件目の削除・ロックの解放）は
        # 素通しする。
        calls["n"] += 1
        if calls["n"] <= UNLINK_ATTEMPTS:
            raise PermissionError("共有違反（注入）")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os_module, "rename", flaky)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    code = run(tmp_path, "release", "GPU0", "--force")

    assert code == EXIT_BROKEN
    assert code != EXIT_OK
    # **消せた分は本当に消えている。** 全滅させたのではなく、部分失敗であることを確かめる。
    assert len(Board(tmp_path).list_for(RESOURCE)) == 1


# --- rb run の自動解放が壊れていないこと -----------------------------------------


def test_run_auto_release_does_not_go_through_cmd_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rb run`` の後始末は ``_cmd_release`` を経由しない（設計どおり）。

    ``_cmd_release`` に nonce 対応の分岐を足しても、ラッパーの後始末
    （``_release_after_run`` → ``board.confirm_own_declaration`` +
    ``board.remove_confirmed``）には一切影響しないはずである。``_cmd_release``
    をスパイに差し替え、**呼ばれた回数**で経路の独立性を直接確かめる
    （``_release_after_run`` は後始末の失敗でジョブの結果を変えないよう例外を
    全て握りつぶすので、「呼ばれたら例外を出す」実装では握りつぶされて検出力を
    持たない）。
    """
    calls: list[object] = []
    original_cmd_release = cli._cmd_release

    def _spy(args: object) -> int:
        calls.append(args)
        return original_cmd_release(args)

    monkeypatch.setattr(cli, "_cmd_release", _spy)

    exit_code = run(
        tmp_path,
        "run",
        "--res",
        "GPU0",
        "--job",
        "自動解放の確認",
        "--observed",
        "調べた",
        "--eta",
        "5m",
        "--",
        sys.executable,
        "-c",
        "pass",
    )

    assert exit_code == 0
    assert calls == []  # `_cmd_release` を一度も経由していない
    assert Board(tmp_path).list_for(RESOURCE) == []


def test_run_auto_release_does_not_re_enumerate_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rb run`` の後始末は、この資源を**列挙し直さない**（issue #18 指摘 2）。

    以前は ``board.remove_own(resource_id, nonce=entry.nonce)`` を呼んでおり、
    ``declared`` を渡さないため内部で ``pairs_for``（資源全体の再列挙）が走って
    いた——自分で作った ``entry`` と ``nonce`` を既に持っているのに、である。
    ``_release_after_run`` を直接呼び、``Board.pairs_for`` /
    ``Board.pairs_for_detailed`` が**1 度も呼ばれないこと**を確かめる
    （``rb run`` 全体を CLI 経由で回すと、取得段階の ``acquire`` 自体が
    ``pairs_for_detailed`` を呼ぶため、後始末だけを切り出して検査する）。
    """
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="再列挙しないことの確認", pid=12345)
    assert board.declare(entry)

    calls: list[str] = []
    original_pairs_for = Board.pairs_for
    original_pairs_for_detailed = Board.pairs_for_detailed

    def spy_pairs_for(self: Board, resource_id: str) -> list:  # type: ignore[type-arg]
        calls.append("pairs_for")
        return original_pairs_for(self, resource_id)

    def spy_pairs_for_detailed(self: Board, resource_id: str) -> object:
        calls.append("pairs_for_detailed")
        return original_pairs_for_detailed(self, resource_id)

    monkeypatch.setattr(Board, "pairs_for", spy_pairs_for)
    monkeypatch.setattr(Board, "pairs_for_detailed", spy_pairs_for_detailed)

    cli._release_after_run(board, RESOURCE, entry, declared=True, exit_code=0)

    # **検証の呼び出し自体が `pairs_for` を使うので、先に判定する。**
    assert calls == [], f"後始末が資源を再列挙した: {calls}"
    assert list((tmp_path / "board").glob("*.json")) == []


# --- rb status に nonce 先頭 8 桁が出ること ---------------------------------------


def test_status_shows_the_nonce_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``rb status`` の出力に、実際の宣言の nonce 先頭 8 桁が現れる。"""
    assert claim(tmp_path, "GPU0", "確認対象") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "status") == EXIT_OK
    out = capsys.readouterr().out

    entry = Board(tmp_path).list_for(RESOURCE)[0]
    assert entry.nonce[:8] in out


# --- 終了コードの契約: コマンド × 故障結果（issue #17 指摘 5） ---------------------


def test_exit_wait_broken_alias_still_exists_and_equals_exit_broken() -> None:
    """``EXIT_WAIT_BROKEN`` は別名として残っている。

    値 3 を ``EXIT_BROKEN`` へ一般化したとき、シンボルごと削除すると
    ``from resource_broker.cli import EXIT_WAIT_BROKEN`` としていた Python 側の
    利用者を壊す（issue #17 指摘 7）。新規のコードは ``EXIT_BROKEN`` を使うが、
    旧名も同じ値を指す別名として引き続き import できることを固定する。
    """
    from resource_broker.cli import EXIT_BROKEN, EXIT_WAIT_BROKEN

    assert EXIT_WAIT_BROKEN == EXIT_BROKEN == 3


def test_an_internal_error_during_release_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release`` の内部エラーは ``EXIT_OK`` ではなく ``EXIT_BROKEN``。

    ``release`` は破壊的操作である。catch-all が 0 を返すと、宣言を 1 件も
    消せていないのに「解放した」と読まれる——フックと非破壊コマンド
    （status / claim / update / history）の catch-all は引き続き 0 のままで
    よいが、``release`` は違う（issue #17 指摘 5）。
    """
    assert claim(tmp_path, "GPU0", "対象") == EXIT_OK

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("release の内部が壊れた")

    monkeypatch.setattr(cli, "_release_own", explode)

    assert run(tmp_path, "release", "GPU0") == EXIT_BROKEN


def test_release_exit_codes_match_the_command_by_outcome_table() -> None:
    """**コマンド × 故障結果**の対応を固定する。定数の値だけでは検出力が無い。

    定数値（``EXIT_BROKEN == 3`` 等）を固定するテストは、値がそろっていれば
    通ってしまい、「``--force`` の完了未確認が実は ``EXIT_OK`` を返している」
    という配線ミスを検出できない。ここでは実際に CLI を呼び、**各経路が
    正しい定数を返しているか**を 1 か所で並べて確認する。
    """
    scenarios: list[tuple[list[str], int]] = []

    def whole_board_unreadable(tmp_path: Path) -> None:
        (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    import tempfile

    for args in (
        ["release", "GPU0"],
        ["release", "GPU0", "--all"],
        ["release", "GPU0", "--force"],
        ["release", "--nonce", "aaaaaaaa"],
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            whole_board_unreadable(tmp_path)
            code = run(tmp_path, *args)
            scenarios.append((args, code))

    for args, code in scenarios:
        assert code == EXIT_BROKEN, f"{args} が読めない掲示板を {code} で通した"
        assert code not in (EXIT_OK, EXIT_BUSY, EXIT_USAGE)

    # **`--clean` も、走査そのものができなければ ``EXIT_BROKEN``。**
    # 「壊れたファイルを消すための経路だから完全性を問わない」のではない
    # ——**走査すらできなかった場合に「読めないファイルはありませんでした」
    # ＋ ``EXIT_OK`` と積極的な成功表現を返していたのが欠陥そのものである**
    # （issue #18 指摘 5。旧版のこのテストはこの誤った挙動を仕様として
    # 固定していた——Codex いわく「盲点ではなく、問題のある意味をテストが
    # 固定している」）。個別の壊れたファイルを掃除する経路自体は変わらず
    # 対象内（別テストで確認する）。
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        whole_board_unreadable(tmp_path)
        assert run(tmp_path, "release", "--clean") == EXIT_BROKEN
