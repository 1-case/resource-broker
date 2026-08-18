"""掲示板の読み書きと排他性を検証する。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from resource_broker.board import SCHEMA, Board, Entry, build_entry


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
    """同時に「空き」を見て一斉に宣言しても、取れるのは 1 人だけである。

    **これは確率で薄める設計ではない。** 掲示板の作成は ``O_EXCL`` が、幽霊の退去は
    ``os.rename`` による捕獲が決める。どちらも OS が原子性を保証する操作であり、
    ロックは競り合いを減らす最適化にすぎない（取れても取れなくても結論は変わらない）。

    単体では ``try_claim`` の戻り値を、フェイクではロックの 3 値を検証しているが、
    **実プロセスを競らせる検証がここまで無かった**。CLAUDE.md が「取得競合の排他性」を
    性質として名指ししているのに、端から端までは一度も確かめていなかった。
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
