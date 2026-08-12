"""掲示板の読み書きと排他性を検証する。"""

from __future__ import annotations

import json

from resource_broker.board import Board, Entry, build_entry


def test_claim_is_exclusive(board: Board) -> None:
    """同じ資源を 2 回宣言できない（O_EXCL による先着 1 名）。"""
    first = build_entry("pc-a::GPU0", job="1 本目")
    second = build_entry("pc-a::GPU0", job="2 本目")

    assert board.try_claim(first) is True
    assert board.try_claim(second) is False


def test_claim_after_release_succeeds(board: Board) -> None:
    """解放した後は再び宣言できる。"""
    board.try_claim(build_entry("pc-a::GPU0", job="1 本目"))
    board.remove("pc-a::GPU0", reason="テスト")

    assert board.try_claim(build_entry("pc-a::GPU0", job="2 本目")) is True


def test_different_resources_do_not_interfere(board: Board) -> None:
    """資源ごとに 1 ファイルなので、別資源の宣言は互いに影響しない。"""
    assert board.try_claim(build_entry("pc-a::GPU0", job="学習")) is True
    assert board.try_claim(build_entry("pc-a::COM3", job="実機")) is True


def test_round_trip_preserves_declared_fields(board: Board) -> None:
    """宣言した内容がそのまま読み戻せる。"""
    entry = build_entry("pc-a::GPU0", job="E008 sweep", log="runs/e008.log")
    board.try_claim(entry)

    loaded = board.read("pc-a::GPU0")
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
    board.try_claim(entry)

    loaded = board.read("pc-a::GPU0")
    assert loaded is not None
    assert loaded.extra["future_field"] == {"kind": "まだ知らない情報"}
    assert loaded.to_dict()["future_field"] == {"kind": "まだ知らない情報"}


def test_list_all_skips_unreadable_entries(board: Board) -> None:
    """壊れたエントリがあっても、読める分は返す。"""
    board.try_claim(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "broken.json").write_text("{ これは JSON ではない", encoding="utf-8")

    resources = [entry.resource for entry in board.list_all()]
    assert resources == ["pc-a::GPU0"]


def test_remove_reports_absence(board: Board) -> None:
    """存在しない宣言の削除は False を返す（例外にしない）。"""
    assert board.remove("pc-a::GPU0", reason="テスト") is False


def test_audit_records_claim_and_removal(board: Board) -> None:
    """宣言と削除が監査ログに残る（沈黙は成功ではない）。"""
    board.try_claim(build_entry("pc-a::GPU0", job="学習"))
    board.remove("pc-a::GPU0", reason="テストのため")

    lines = [
        json.loads(line)
        for path in board.audit_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    events = {record["event"] for record in lines}
    assert {"claimed", "removed"} <= events
    assert all("at" in record for record in lines)


def test_entry_from_dict_rejects_shapeless_data() -> None:
    """resource が無いデータは Entry にしない。"""
    assert Entry.from_dict({"holder": {}}) is None
    assert Entry.from_dict("文字列") is None
    assert Entry.from_dict(None) is None
