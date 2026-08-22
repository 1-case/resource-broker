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
from resource_broker.board import Board, build_entry
from resource_broker.cli import EXIT_OK, EXIT_USAGE, main
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


# --- rb run の自動解放が壊れていないこと -----------------------------------------


def test_run_auto_release_does_not_go_through_cmd_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rb run`` の後始末は ``_cmd_release`` を経由しない（設計どおり）。

    ``_cmd_release`` に nonce 対応の分岐を足しても、ラッパーの後始末
    （``_release_after_run`` → ``board.remove_own(..., nonce=entry.nonce)``）には
    一切影響しないはずである。``_cmd_release`` をスパイに差し替え、**呼ばれた回数**
    で経路の独立性を直接確かめる（``_release_after_run`` は後始末の失敗でジョブの
    結果を変えないよう例外を全て握りつぶすので、「呼ばれたら例外を出す」実装では
    握りつぶされて検出力を持たない）。
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


# --- rb status に nonce 先頭 8 桁が出ること ---------------------------------------


def test_status_shows_the_nonce_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``rb status`` の出力に、実際の宣言の nonce 先頭 8 桁が現れる。"""
    assert claim(tmp_path, "GPU0", "確認対象") == EXIT_OK
    capsys.readouterr()

    assert run(tmp_path, "status") == EXIT_OK
    out = capsys.readouterr().out

    entry = Board(tmp_path).list_for(RESOURCE)[0]
    assert entry.nonce[:8] in out
