"""掲示板の読み書きと排他性を検証する。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from resource_broker.board import (
    SCHEMA,
    Board,
    Entry,
    RemovalResult,
    build_entry,
)
from resource_broker.naming import normalize


def _first(board: Board, resource_id: str) -> Entry | None:
    """その資源の**最初の宣言**（無ければ None）。順序は ``since``。"""
    found = board.list_for(resource_id)
    return found[0] if found else None


def test_declaring_never_refuses(board: Board) -> None:
    """掲示板は宣言を断らない。**断るのは CLI の判断である。**

    ファイル名が nonce なので ``O_EXCL`` は衝突せず、同じ資源へ何件でも並ぶ。
    「使われているなら断る」は掲示板の仕事ではなく、読んで判断する側の仕事である。
    """
    first = build_entry("pc-a::GPU0", job="1 本目")
    second = build_entry("pc-a::GPU0", job="2 本目")

    assert board.declare(first) is True
    # 宣言は**必ず残せる**（ファイル名は nonce）。断るのは CLI の判断であって
    # 掲示板の仕事ではない。
    assert board.declare(second) is True
    assert len(board.list_for("pc-a::GPU0")) == 2


def test_claim_after_release_succeeds(board: Board) -> None:
    """解放した後は再び宣言できる。"""
    board.declare(build_entry("pc-a::GPU0", job="1 本目"))
    board.remove_all("pc-a::GPU0", reason="テスト")

    assert board.declare(build_entry("pc-a::GPU0", job="2 本目")) is True


def test_different_resources_do_not_interfere(board: Board) -> None:
    """資源ごとに 1 ファイルなので、別資源の宣言は互いに影響しない。"""
    assert board.declare(build_entry("pc-a::GPU0", job="学習")) is True
    assert board.declare(build_entry("pc-a::COM3", job="実機")) is True


def test_round_trip_preserves_declared_fields(board: Board) -> None:
    """宣言した内容がそのまま読み戻せる。"""
    entry = build_entry("pc-a::GPU0", job="E008 sweep", log="runs/e008.log")
    board.declare(entry)

    loaded = _first(board, "pc-a::GPU0")
    assert loaded is not None
    assert loaded.job == "E008 sweep"
    assert loaded.log == "runs/e008.log"
    assert loaded.resource == "pc-a::GPU0"


def test_timestamps_are_machine_generated(board: Board) -> None:
    """宣言時刻はオフセット付きで自動生成される（呼び出し側に書かせない）。"""
    entry = build_entry("pc-a::GPU0", job="学習")

    assert entry.since_dt is not None
    assert entry.since_dt.tzinfo is not None


def test_unknown_fields_survive_a_read_write_cycle(board: Board) -> None:
    """未知のフィールドを消さない（前方互換）。

    新しいバージョンが書いたフィールドを古いバージョンが読んで書き戻しても
    失われないことを保証する。
    """
    entry = build_entry("pc-a::GPU0", job="学習")
    entry.extra["future_field"] = {"kind": "まだ知らない情報"}
    board.declare(entry)

    loaded = _first(board, "pc-a::GPU0")
    assert loaded is not None
    assert loaded.extra["future_field"] == {"kind": "まだ知らない情報"}
    assert loaded.to_dict()["future_field"] == {"kind": "まだ知らない情報"}


def test_list_all_skips_unreadable_entries(board: Board) -> None:
    """壊れたエントリがあっても、読める分は返す。"""
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "broken.json").write_text("{ これは JSON ではない", encoding="utf-8")

    resources = [entry.resource for entry in board.list_all()]
    assert resources == ["pc-a::GPU0"]


def test_remove_reports_absence(board: Board) -> None:
    """存在しない宣言の削除は False を返す（例外にしない）。"""
    assert board.remove_all("pc-a::GPU0", reason="テスト") == 0


def test_audit_records_claim_and_removal(board: Board) -> None:
    """宣言と削除が監査ログに残る（沈黙は成功ではない）。"""
    board.declare(build_entry("pc-a::GPU0", job="学習"))
    board.remove_all("pc-a::GPU0", reason="テストのため")

    lines = [
        json.loads(line)
        for path in board.audit_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    events = {record["event"] for record in lines}
    assert {"claimed", "removed"} <= events
    assert all("at" in record for record in lines)


def test_audit_lines_stay_valid_json_when_too_long(board: Board) -> None:
    """長すぎるレコードは**切らずに畳む**。

    途中で切ると JSON として壊れ、その行は読む側から丸ごと消える
    （`rb history` は壊れた行を飛ばす）。長さの判定は文字数ではなく
    **エンコード後のバイト数**で行う（日本語は 1 文字 3 バイト）。
    """
    from resource_broker import audit

    board.audit("claimed", resource="pc-a::GPU0", note="あ" * 5000)

    lines = [
        line
        for path in board.audit_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    records = [json.loads(line) for line in lines]  # 壊れていれば例外になる

    assert any(record.get("truncated") for record in records)
    assert all(len((line + "\n").encode("utf-8")) <= audit.MAX_LINE_BYTES for line in lines)
    assert all(record.get("event") for record in records)  # 何が起きたかは残る


def test_short_audit_lines_are_kept_whole(board: Board) -> None:
    """短いレコードはそのまま残す（畳むのは上限を超えたときだけ）。"""
    board.audit("claimed", resource="pc-a::GPU0", note="compute apps なし")

    records = [
        json.loads(line)
        for path in board.audit_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert records[0]["note"] == "compute apps なし"
    assert "truncated" not in records[0]


def test_entry_from_dict_rejects_shapeless_data() -> None:
    """resource が無いデータは Entry にしない。"""
    assert Entry.from_dict({"holder": {}}) is None
    assert Entry.from_dict("文字列") is None
    assert Entry.from_dict(None) is None


# --- 実プロセスで競らせる ---------------------------------------------------------


def _race_claim(index: int, home: Path, barrier: threading.Barrier, codes: list[int]) -> None:
    """バリアで待ち合わせてから ``rb claim`` を打つ。"""
    env = dict(os.environ)
    # **それぞれ別のセッションとして名乗る。** 同じ session_id だと所有判定が
    # 「自分のもの」に倒れ、競争そのものが起きなくなる。
    env["RESOURCE_BROKER_SESSION_ID"] = f"racer-{index}"
    argv = [
        sys.executable,
        "-m",
        "resource_broker.cli",
        "--home",
        str(home),
        "claim",
        "GPU0",
        "--job",
        f"競争 {index}",
        "--observed",
        "全員が同じ「空き」を見た",
        "--eta",
        "10m",
        "--found",
        "free",
    ]
    barrier.wait()
    codes[index] = subprocess.run(argv, capture_output=True, env=env, timeout=120).returncode


def test_only_one_of_many_simultaneous_claims_wins(tmp_path: Path) -> None:
    """同時に「空き」を見て一斉に宣言しても、通るのは 1 人だけである。

    **これは保証ではなく、ロックが取れる間の振る舞いである。** 宣言のファイル名は
    nonce なので ``O_EXCL`` は取得競合を解決しない。直列化しているのは資源ごとの
    ロックだけで、**取れなければ全員通る**（別のテストがその劣化を固定している）。

    ここで確かめるのは「既定の経路では 1 人だけが通る」ことである。端から端まで、
    実プロセスを競らせて見る。
    """
    racers = 5
    codes: list[int] = [-1] * racers
    barrier = threading.Barrier(racers)
    threads = [
        threading.Thread(target=_race_claim, args=(i, tmp_path, barrier, codes))
        for i in range(racers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert codes.count(0) == 1, f"取得できたのが 1 人ではない: {codes}"
    assert codes.count(1) == racers - 1, f"残りが使用中で断られていない: {codes}"
    assert len(list((tmp_path / "board").glob("*.json"))) == 1


# --- 前方互換: 既知のキーの「未知の形」も落とさない -------------------------------


def test_a_known_key_with_an_unexpected_type_is_not_silently_dropped() -> None:
    """既知のキーが想定外の型で来ても、読んで書き戻したときに消えない。

    前方互換は「未知のキー」だけでは足りない。既知のキーが想定外の型で来ると
    既定値へ倒れ、``extra`` にも入らないため、**スキーマを拡張した新しい版が書いた値を
    古い版が読んで書き戻した瞬間に黙って消す**。
    """
    entry = Entry.from_dict(
        {
            "resource": "pc-a::GPU0",
            "sharing": {"allowed": True, "limit": "5GB"},  # 将来 dict 化した想定
            "display": ["GPU0", "RTX"],
            "未知のキー": 1,
        }
    )

    assert entry is not None
    assert entry.sharing == ""  # 型が合わないので既定値へ倒れる
    assert entry.extra["x-sharing"] == {"allowed": True, "limit": "5GB"}, "退避されていない"
    assert entry.extra["x-display"] == ["GPU0", "RTX"]
    assert entry.extra["未知のキー"] == 1  # 従来の前方互換も効いている

    # 書き戻しても失われない
    assert entry.to_dict()["x-sharing"] == {"allowed": True, "limit": "5GB"}


def test_reading_and_writing_twice_is_a_fixed_point() -> None:
    """読んで書き戻す往復を 2 周しても、退避した値が増えも減りもしない。

    片道だけ確かめても、**2 周目で ``x-sharing`` がさらに ``x-x-sharing`` へ包まれる**
    ような実装は通ってしまう。掲示板は版が混ざったまま何度も読み書きされる。
    """
    original = {
        "schema": 2,
        "resource": "pc-a::GPU0",
        "sharing": {"allowed": True},
        "未知のキー": 1,
    }

    first = Entry.from_dict(original)
    assert first is not None
    once = first.to_dict()

    second = Entry.from_dict(once)
    assert second is not None
    twice = second.to_dict()

    assert twice == once, "往復のたびに形が変わっている"


def test_a_newer_schema_marker_is_not_downgraded() -> None:
    """新しい版が書いたスキーマ番号を、古い版が読んで書き戻しても下げない。

    未知のフィールドは ``extra`` が保つのに版の印だけ消すと、次に読む側は
    「古い形のエントリに知らない鍵が混じっている」と見ることになる。
    """
    entry = Entry.from_dict({"schema": 99, "resource": "pc-a::GPU0", "未来の鍵": "値"})

    assert entry is not None
    assert entry.to_dict()["schema"] == 99
    assert entry.to_dict()["未来の鍵"] == "値"


def test_a_broken_schema_marker_falls_back_to_the_current_version() -> None:
    """壊れた版番号は現行版として扱う（bool は int の派生なので明示的に除く）。"""
    for broken in (True, "2", None, 0, -1, [2]):
        entry = Entry.from_dict({"schema": broken, "resource": "pc-a::GPU0"})
        assert entry is not None
        assert entry.to_dict()["schema"] == SCHEMA, broken

    # **倒した元の値は捨てない。** 退避しないと、書き戻した瞬間に黙って消える
    # ——この仕組みが防ごうとしている当のことを自分でやることになる。
    # （``None`` は「書かれていない」であって壊れた値ではないので対象外。）
    for broken in (True, "2", 0, -1, [2]):
        entry = Entry.from_dict({"schema": broken, "resource": "pc-a::GPU0"})
        assert entry is not None
        assert entry.to_dict().get("x-schema") == broken, broken


def test_salvage_does_not_overwrite_an_existing_extension_field() -> None:
    """``x-`` は拡張フィールドの慣例接頭辞である。既にある値を退避で潰さない。

    潰せば、この仕組みが防ごうとしている取りこぼしを自分で起こす。
    """
    entry = Entry.from_dict(
        {
            "resource": "pc-a::GPU0",
            "sharing": {"allowed": True},
            "x-sharing": "別の版が書いた値",
        }
    )

    assert entry is not None
    assert entry.extra["x-sharing"] == "別の版が書いた値", "既存の拡張を潰している"


def test_without_the_lock_two_claims_both_get_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**ロックが取れなければ排他は無い。** その劣化をここで固定する。

    宣言のファイル名は nonce なので ``O_EXCL`` は取得競合を解決しない。直列化して
    いるのは資源ごとのロックだけで、取れなければ「読む → 読み直す → 書く」が交錯し、
    2 人とも通る。**保証の範囲を誤解させないために、劣化のほうも書いておく。**

    この性質は隣の `test_only_one_of_many_simultaneous_claims_wins` と対になっている
    ——あちらは「ロックが取れる既定の経路では 1 人だけ」、こちらは「取れなければ全員」。
    """
    from contextlib import contextmanager

    from resource_broker.board import LockState
    from resource_broker.cli import main

    @contextmanager
    def unavailable(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield LockState.UNAVAILABLE

    monkeypatch.setattr(Board, "locked", unavailable)

    board = Board(tmp_path)
    ready = threading.Barrier(2)
    real_declare = Board.declare

    def declare_together(self, entry):  # type: ignore[no-untyped-def]
        ready.wait(timeout=5)  # 2 人とも「空きだ」と読み終えてから書く
        return real_declare(self, entry)

    monkeypatch.setattr(Board, "declare", declare_together)

    codes: list[int] = [-1, -1]

    def claim(index: int) -> None:
        codes[index] = main(
            [
                "--home",
                str(tmp_path),
                "claim",
                "GPU0",
                "--job",
                f"J{index}",
                "--observed",
                "空だった",
                "--eta",
                "1h",
                "--found",
                "free",
            ]
        )

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert codes == [0, 0], "ロック無しでも排他が効いてしまっている（前提が変わった）"
    assert len(board.list_for(normalize("GPU0"))) == 2


def test_a_declaration_in_the_legacy_layout_is_read(tmp_path: Path) -> None:
    """旧い置き場（``board/<資源>.json`` と ``board/joins/*.json``）を宣言として読む。

    形式を変えた瞬間に、稼働中のセッションの宣言を見失わないための経路である。
    **ここが消えても本番の掲示板からしか気づけない**ので、テストで固定する。
    """
    board = Board(tmp_path)
    (tmp_path / "board" / "joins").mkdir(parents=True)
    old_primary = build_entry("pc-a::GPU0", job="旧い主宣言", session="old")
    old_join = build_entry("pc-a::GPU0", job="旧い相乗り", session="old2")
    (tmp_path / "board" / "pc-a__GPU0.json").write_text(
        json.dumps(old_primary.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "board" / "joins" / "何か.json").write_text(
        json.dumps(old_join.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    assert sorted(e.job for e in board.list_for("pc-a::GPU0")) == ["旧い主宣言", "旧い相乗り"]


def test_a_declaration_in_the_legacy_layout_can_be_removed(tmp_path: Path) -> None:
    """旧い置き場の宣言を**消せる**（nonce があれば CAS、無ければ中身の照合）。"""
    board = Board(tmp_path)
    (tmp_path / "board" / "joins").mkdir(parents=True)
    with_nonce = build_entry("pc-a::GPU0", job="nonce あり", session="old")
    without = build_entry("pc-a::GPU0", job="nonce なし", session="old2")
    without.holder.pop("nonce", None)
    (tmp_path / "board" / "pc-a__GPU0.json").write_text(
        json.dumps(with_nonce.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "board" / "joins" / "何か.json").write_text(
        json.dumps(without.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    assert (
        board.remove_if_nonce("pc-a::GPU0", expect_nonce=with_nonce.nonce, reason="テスト")
        is RemovalResult.REMOVED
    )

    path, entry = board.pairs_for("pc-a::GPU0")[0]
    assert board.remove_matching(path, entry, reason="テスト") is RemovalResult.REMOVED
    assert board.list_for("pc-a::GPU0") == []


def test_an_empty_nonce_is_never_a_cas_key(tmp_path: Path) -> None:
    """空の nonce を照合鍵にしない。

    「nonce が空のもの」に一致させると、nonce を持たない古い宣言が 2 件あるとき
    **別の生きた宣言**を捕まえて消す（古い順の先頭が当たる）。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True)
    for index in range(2):
        entry = build_entry("pc-a::GPU0", job=f"古い宣言{index}", session=f"s{index}")
        entry.holder.pop("nonce", None)
        (board.entries_dir / f"old{index}.json").write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False), encoding="utf-8"
        )

    assert board.remove_if_nonce("pc-a::GPU0", expect_nonce="", reason="テスト") is (
        RemovalResult.NOT_OWNED
    )
    assert len(board.list_for("pc-a::GPU0")) == 2, "空の nonce で宣言が消えた"
