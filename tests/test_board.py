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
    BoardListing,
    Entry,
    PartialListingError,
    RemovalResult,
    build_entry,
)
from resource_broker.naming import normalize


def _first(board: Board, resource_id: str) -> Entry | None:
    """その資源の**最初の宣言**（無ければ None）。順序は ``since``。"""
    found = board.list_for(resource_id)
    return found[0] if found else None


def _wipe(board: Board, resource_id: str, *, reason: str = "テスト") -> int:
    """テストの後始末用: その資源の宣言を全部消す（``--force`` 相当）。消せた件数を返す。

    ``Board.remove_all`` は廃止した——資源名だけで何件消えるか決まる公開入口は
    持たない（型で強制する設計）。テストは列挙してから
    :meth:`Board.remove_selected` へ渡す形に合わせる。
    """
    selections = board.pairs_for_detailed(resource_id).confirmed()
    result = board.remove_selected(resource_id, selections, reason=reason)
    return len(result.removed)


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
    _wipe(board, "pc-a::GPU0")

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
    """存在しない宣言の削除は 0 件を返す（例外にしない）。"""
    assert _wipe(board, "pc-a::GPU0") == 0


def test_audit_records_claim_and_removal(board: Board) -> None:
    """宣言と削除が監査ログに残る（沈黙は成功ではない）。"""
    board.declare(build_entry("pc-a::GPU0", job="学習"))
    _wipe(board, "pc-a::GPU0", reason="テストのため")

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
            "boot": 12345,  # 将来 数値化した想定
            "未知のキー": 1,
        }
    )

    assert entry is not None
    assert entry.sharing == ""  # 型が合わないので既定値へ倒れる
    assert entry.extra["x-sharing"] == {"allowed": True, "limit": "5GB"}, "退避されていない"
    assert entry.extra["x-boot"] == 12345
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


def test_a_declaration_in_the_legacy_fixed_path_is_still_read(tmp_path: Path) -> None:
    """旧い固定パス形式（``board/<資源>.json``）の宣言は、``board/`` 直下にあれば読める。

    平坦化後もこの走査（``board/`` 直下の ``*.json``）自体は変わっていないので、
    ファイル名が資源由来の固定名であっても、単に 1 件の宣言として読める。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True)
    old_primary = build_entry("pc-a::GPU0", job="旧い宣言", session="old")
    (board.entries_dir / "pc-a__GPU0.json").write_text(
        json.dumps(old_primary.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    assert [e.job for e in board.list_for("pc-a::GPU0")] == ["旧い宣言"]


def test_the_legacy_joins_directory_is_no_longer_scanned(tmp_path: Path) -> None:
    """``board/joins/`` はもう走査しない。

    宣言の寿命を監査ログで実測すると中央値 5.4 分・最長 2.1 時間・24 時間超はゼロ
    だった（issue #9）。その短い窓のためだけに別ディレクトリの走査を残す理由が
    無く、実際にそこから複数の欠陥が出ていた（issue #19 指摘 8・10、issue #18
    指摘 3）。**ここが変わっても本番の掲示板からしか気づけない**ので、テストで
    固定する。
    """
    board = Board(tmp_path)
    (tmp_path / "board" / "joins").mkdir(parents=True)
    old_join = build_entry("pc-a::GPU0", job="旧い相乗り", session="old2")
    (tmp_path / "board" / "joins" / "何か.json").write_text(
        json.dumps(old_join.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    assert board.list_for("pc-a::GPU0") == []


def test_a_declaration_without_a_nonce_is_still_listed(tmp_path: Path) -> None:
    """nonce を持たない宣言（旧形式）も ``board/`` 直下にあれば見える。

    「見えないまま残る」を作らないための最低条件——消す手段が ``--force`` /
    ``--clean`` に絞られても、``rb status`` の元になる列挙には出続ける（issue #9）。
    """
    board = Board(tmp_path)
    entry = build_entry("pc-a::GPU0", job="nonce なし", session="old")
    entry.holder.pop("nonce", None)
    board.entries_dir.mkdir(parents=True)
    (board.entries_dir / "pc-a__GPU0.json").write_text(
        json.dumps(entry.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    found = board.list_for("pc-a::GPU0")

    assert [e.job for e in found] == ["nonce なし"]
    assert found[0].nonce == ""


def test_a_declaration_without_a_nonce_is_not_removed_without_force(tmp_path: Path) -> None:
    """nonce を持たない宣言は、個体として指せないので通常の削除経路では消えない。

    ``_remove_matching``（中身の照合で消す経路）を削除した代わりに、
    :meth:`Board.remove_confirmed` は ``force=True`` を渡さない限りこの形の宣言を
    拒否する（issue #9）。
    """
    board = Board(tmp_path)
    entry = build_entry("pc-a::GPU0", job="nonce なし", session="old")
    entry.holder.pop("nonce", None)
    board.entries_dir.mkdir(parents=True)
    (board.entries_dir / "pc-a__GPU0.json").write_text(
        json.dumps(entry.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (selection,) = board.pairs_for_detailed("pc-a::GPU0").confirmed()

    assert board.remove_confirmed(selection, reason="テスト") is RemovalResult.NOT_OWNED
    assert len(board.list_for("pc-a::GPU0")) == 1  # 消えていない


def test_a_declaration_without_a_nonce_can_be_removed_with_force(tmp_path: Path) -> None:
    """``--force``（``remove_confirmed(force=True)``）だけがこの形の宣言を消せる。"""
    board = Board(tmp_path)
    entry = build_entry("pc-a::GPU0", job="nonce なし", session="old")
    entry.holder.pop("nonce", None)
    board.entries_dir.mkdir(parents=True)
    (board.entries_dir / "pc-a__GPU0.json").write_text(
        json.dumps(entry.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (selection,) = board.pairs_for_detailed("pc-a::GPU0").confirmed()

    result = board.remove_confirmed(selection, reason="テスト", force=True)

    assert result is RemovalResult.REMOVED
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

    assert board._remove_if_nonce("pc-a::GPU0", expect_nonce="", reason="テスト") is (
        RemovalResult.NOT_OWNED
    )
    assert len(board.list_for("pc-a::GPU0")) == 2, "空の nonce で宣言が消えた"


# --- 候補集合の完全性（issue #17 指摘 1・2） ---------------------------------------
#
# 「候補集合は完全か」は破壊的操作の全経路に共通する性質である。ここでは
# ``declarations_detailed`` / ``pairs_for_detailed`` が返す ``BoardListing.complete``
# が、**理由を問わず**（読めない・壊れている・構造が不正・不正な UTF-8）
# ``False`` になることを 1 か所で固定する。CLI 側の経路別テストは
# tests/test_release_nonce.py・tests/test_waiting.py に置く。


def test_declarations_detailed_is_incomplete_on_a_permission_style_failure(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読めないファイル（I/O 失敗）があれば ``complete=False``。"""
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    flaky = board.entries_dir / "読めない.json"
    flaky.write_text("{}", encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "読めない.json":
            raise PermissionError("共有違反")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)

    listing = board.declarations_detailed()

    assert listing.complete is False
    # **読めたものは捨てない。** 完全性が崩れても、読めた宣言は返す
    # （読める分は使えるという fail-open の原則は完全性の判定と別物である）。
    assert [e.job for _, e in listing.pairs] == ["正常"]


def test_declarations_detailed_is_incomplete_on_json_corruption(board: Board) -> None:
    """JSON として壊れているファイル（``entry_corrupt``）があれば ``complete=False``。

    以前はここを見ていなかった——``entry_unreadable``（I/O 失敗）だけが完全性を
    崩し、JSON の破損は「完全に読めた」側に黙って含まれていた（issue #17 指摘 1）。
    """
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "壊れている.json").write_text("{ これは JSON ではない", encoding="utf-8")

    listing = board.declarations_detailed()

    assert listing.complete is False
    assert [e.job for _, e in listing.pairs] == ["正常"]


def test_declarations_detailed_is_incomplete_on_structural_corruption(board: Board) -> None:
    """JSON としては正しいが、宣言の最低限の形を満たさないファイルも ``complete=False``。

    ``resource`` が読めない以上、この 1 件がどの資源のものか分からない
    ——理由が「JSON が壊れている」ではなく「必須フィールドが無い」だけの違いで、
    危険の中身（探している宣言が隠れているかもしれない）は変わらない。
    """
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "resource無し.json").write_text(
        json.dumps({"schema": 1, "holder": {}}), encoding="utf-8"
    )

    listing = board.declarations_detailed()

    assert listing.complete is False
    assert [e.job for _, e in listing.pairs] == ["正常"]


def test_declarations_detailed_is_incomplete_on_invalid_utf8(board: Board) -> None:
    """不正な UTF-8 バイト列のファイルでも例外を投げず、``complete=False`` にする。

    以前は ``read_text`` を ``OSError`` だけで囲っていたため、``UnicodeDecodeError``
    （``ValueError`` の派生）が素通りして本モジュールの「公開関数は例外を投げない」
    という約束を破っていた（issue #17 指摘 1 の「不正な UTF-8」）。
    """
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "不正utf8.json").write_bytes(b"\xff\xfe\x00broken")

    listing = board.declarations_detailed()  # 例外を投げないことそのものが検査対象

    assert listing.complete is False
    assert [e.job for _, e in listing.pairs] == ["正常"]


def test_declarations_detailed_is_incomplete_even_after_an_earlier_success(
    board: Board,
) -> None:
    """**最初の 1 件が読めたことは、後続の失敗を覆い隠さない。**

    走査は辞書順に進む。壊れたファイル名を読める宣言より**後**に置き、
    「最初の成功だけを見て complete=True のまま確定する」実装を落とすための
    退行注入である（辞書順で `a-first` が `z-broken` より先に走査される）。
    """
    board.declare(build_entry("pc-a::GPU0", job="先に読める方"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    # 宣言のファイル名は nonce の安全化なので、既存の 1 件より確実に後に来る名前を選ぶ。
    (board.entries_dir / "zzz-壊れている.json").write_text("not json", encoding="utf-8")

    listing = board.declarations_detailed()

    assert listing.complete is False
    assert [e.job for _, e in listing.pairs] == ["先に読める方"]


def test_pairs_for_detailed_is_incomplete_for_an_unrelated_corrupt_file(
    board: Board,
) -> None:
    """**別の資源にしか見えない壊れたファイルでも、完全性は崩れる。**

    壊れたファイルは中身が読めないので、``resource`` も分からない——絞り込む
    「前」の段階で情報が失われている。資源で絞ってから完全性を見ると、絞る前に
    失われた情報を「たまたま全部読めた」と取り違える（issue #17 指摘 1）。
    """
    board.declare(build_entry("pc-a::GPU0", job="無関係な資源"))
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "壊れている.json").write_text("not json", encoding="utf-8")

    listing = board.pairs_for_detailed("pc-a::COM3")

    assert listing.complete is False
    assert listing.pairs == []  # COM3 自体の宣言は無い


def test_pairs_for_detailed_is_complete_when_nothing_is_broken(board: Board) -> None:
    """壊れたものが無ければ ``complete=True``。**畳んで恒真にしていないか**の対照。"""
    board.declare(build_entry("pc-a::GPU0", job="正常"))

    listing = board.pairs_for_detailed("pc-a::GPU0")

    assert listing.complete is True
    assert [e.job for _, e in listing.pairs] == ["正常"]


# --- 型による強制: 完全性を確認していない状態から削除できない（issue #18） --------
#
# ここが今回の設計の核心である。低水準の CAS（`_remove_if_nonce`）
# は private にし、個別の宣言を消す唯一の公開入口 `remove_confirmed` は
# `ConfirmedEntry` を要求する。`ConfirmedEntry` を作れるのは
# `BoardListing.confirmed()`（完全性を確認した列挙からのみ、`complete=False` なら
# 例外）と `Board.confirm_own_declaration()`（自分が書いた宣言）の 2 か所だけである。
# 「完全性の確認を忘れる」という 3 回繰り返した欠陥の形が、そもそも書けなくなって
# いることを固定する。


def test_listing_confirmed_refuses_to_hand_out_selections_when_incomplete() -> None:
    """**不完全な列挙からは、削除できる選択を 1 件も作れない。**

    ``BoardListing.confirmed()`` は ``complete=False`` なら
    ``PartialListingError`` を送出する。これが「完全性を確認し忘れたまま
    削除へ進む」という経路そのものを塞ぐ——``ConfirmedEntry`` を得る手段が
    他に無いので、確認を飛ばしたコードは実行時に必ず落ちる。
    """
    entry = build_entry("pc-a::GPU0", job="読めなかった側に隠れているかもしれない宣言")
    incomplete = BoardListing(pairs=[(Path("dummy.json"), entry)], complete=False)

    with pytest.raises(PartialListingError):
        incomplete.confirmed()


def test_listing_confirmed_succeeds_when_complete(board: Board) -> None:
    """対照: 完全な列挙からは、パスと同じ並び・同じ長さで選択が作れる。"""
    board.declare(build_entry("pc-a::GPU0", job="正常"))
    listing = board.pairs_for_detailed("pc-a::GPU0")

    selections = listing.confirmed()

    assert [s.entry.job for s in selections] == ["正常"]
    assert [s.path for s in selections] == [path for path, _ in listing.pairs]


def test_remove_confirmed_refuses_anything_that_is_not_a_confirmed_entry(
    board: Board,
) -> None:
    """**公開の削除入口は ``ConfirmedEntry`` でなければ呼べない。**

    生の ``(Path, Entry)`` タプルや、それらしく見える別の型を渡しても、
    ``TypeError`` で拒否する。渡すものが手元に無ければ削除できない
    ——「完全性を確認したつもり」で実は確認していない状態を、型で締め出す。
    """
    entry = build_entry("pc-a::GPU0", job="対象")
    board.declare(entry)
    path = board.pairs_for("pc-a::GPU0")[0][0]

    with pytest.raises(TypeError):
        board.remove_confirmed((path, entry), reason="テスト")  # type: ignore[arg-type]

    # 拒否しただけで、何も消えていないことも確かめる。
    assert board.list_for("pc-a::GPU0") != []


def test_remove_confirmed_accepts_a_confirmed_entry_from_the_listing(board: Board) -> None:
    """対照: ``BoardListing.confirmed()`` で得た選択はそのまま渡して消せる。"""
    board.declare(build_entry("pc-a::GPU0", job="対象"))
    (selection,) = board.pairs_for_detailed("pc-a::GPU0").confirmed()

    assert board.remove_confirmed(selection, reason="テスト") is RemovalResult.REMOVED
    assert board.list_for("pc-a::GPU0") == []


def test_confirm_own_declaration_needs_no_complete_listing(board: Board) -> None:
    """``confirm_own_declaration`` は列挙を経由しない——自分が書いた実体を直接指せる。

    ``rb run`` の後始末はこれを使う。列挙を経由しないので、掲示板の他の部分が
    壊れていても（このテストではそもそも他に何も無いが）影響を受けない。
    """
    entry = build_entry("pc-a::GPU0", job="自分が書いた宣言")
    assert board.declare(entry)

    selection = board.confirm_own_declaration(entry)

    assert board.remove_confirmed(selection, reason="テスト") is RemovalResult.REMOVED


def test_remove_own_cannot_be_called_without_declared() -> None:
    """``remove_own`` は ``declared``（完全性を確認済みの選択）が必須である。

    以前は ``None`` 許容の任意引数で、渡さないと内部で ``pairs_for``
    （完全性を捨てる再列挙）が走っていた——``rb run`` の自動解放がまさに
    これを渡し忘れていた（issue #18 指摘 2）。必須にすることで「渡し忘れる」
    という事態そのものが ``TypeError`` になる。
    """
    with pytest.raises(TypeError):
        Board(Path("dummy")).remove_own(  # type: ignore[call-arg]
            "pc-a::GPU0", reason="テスト", nonce="なんでも"
        )


def test_remove_selected_requires_a_list_of_confirmed_entries(board: Board) -> None:
    """``remove_selected``（``--force`` の実体）も、確認済みの選択の並びを要求する。

    渡した並び**以外**は対象にならない——資源名だけで何件消えるか決まる
    公開入口はここには無い。並びの中身が ``ConfirmedEntry`` でなければ、
    その 1 件で ``TypeError`` になる（``remove_confirmed`` が内部で検査する）。
    """
    entry = build_entry("pc-a::GPU0", job="対象")
    board.declare(entry)
    path = board.pairs_for("pc-a::GPU0")[0][0]

    with pytest.raises(TypeError):
        board.remove_selected("pc-a::GPU0", [(path, entry)], reason="テスト")  # type: ignore[list-item]


# --- known を渡した CAS: ABSENT と「確認できない」を分ける（issue #17 指摘 2・3） ---


def test_remove_if_nonce_with_known_removes_the_exact_entity(board: Board) -> None:
    """``known`` を渡すと、再列挙せずにその実体を消せる。"""
    entry = build_entry("pc-a::GPU0", job="対象")
    board.declare(entry)
    path, found = board.pairs_for("pc-a::GPU0")[0]

    result = board._remove_if_nonce(
        "pc-a::GPU0", expect_nonce=entry.nonce, reason="テスト", known=(path, found)
    )

    assert result is RemovalResult.REMOVED
    assert board.list_for("pc-a::GPU0") == []


def test_remove_if_nonce_with_known_distinguishes_absent_from_swapped(
    board: Board,
) -> None:
    """``known`` の実体が消えていても、**別の宣言に入れ替わっていれば「無い」と言わない**。

    選択に使った実体をそのまま渡すだけでは、その実体が消えたあとに**別の宣言**が
    同じ資源へ現れていた場合を見落とす。以前の ``pairs_for`` ベースの先読みが
    守っていた区別（issue #15 で入れた ABSENT / NOT_OWNED の非対称）を、
    ``known`` 経路でも保つことを固定する。
    """
    board.declare(build_entry("pc-a::GPU0", job="消える方"))
    gone_path, gone_entry = board.pairs_for("pc-a::GPU0")[0]
    assert (
        board._remove_if_nonce("pc-a::GPU0", expect_nonce=gone_entry.nonce, reason="事前に消す")
        is RemovalResult.REMOVED
    )
    # 消えた直後に、別の宣言が現れる（他セッションが取り直した想定）。
    board.declare(build_entry("pc-a::GPU0", job="入れ替わった方"))

    result = board._remove_if_nonce(
        "pc-a::GPU0", expect_nonce=gone_entry.nonce, reason="テスト", known=(gone_path, gone_entry)
    )

    assert result is RemovalResult.NOT_OWNED, "入れ替わりを「無い」と言っている"
    assert [e.job for e in board.list_for("pc-a::GPU0")] == ["入れ替わった方"]


def test_remove_if_nonce_with_known_returns_absent_when_truly_nothing_remains(
    board: Board,
) -> None:
    """``known`` の実体が消え、他に何も残っていなければ ``ABSENT``。"""
    board.declare(build_entry("pc-a::GPU0", job="唯一の宣言"))
    path, entry = board.pairs_for("pc-a::GPU0")[0]
    assert (
        board._remove_if_nonce("pc-a::GPU0", expect_nonce=entry.nonce, reason="事前に消す")
        is RemovalResult.REMOVED
    )

    result = board._remove_if_nonce(
        "pc-a::GPU0", expect_nonce=entry.nonce, reason="テスト", known=(path, entry)
    )

    assert result is RemovalResult.ABSENT


def test_remove_if_nonce_known_absent_recheck_that_cannot_read_is_not_reported_as_absent(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**削除の直前に掲示板が読めなくなったら、「無い」と言わない。**

    ``known`` の実体が消えていたので確かめ直そうとしたら、その確かめ直し自体が
    読めなかった——このとき「本当に無い」と「確認できない」を混同すると、実は
    生きている別の宣言を見落として「解放した」と嘘をつくことになる（issue #17
    指摘 2 の「削除直前の読取失敗」）。``UNCONFIRMED`` という独立の値に倒す
    ——``FAILED``（掲示板は読めた上で I/O が失敗した）と畳むと、CLI の終了
    コードが「使用中で消せなかった」に化ける（issue #18 指摘 4）。
    """
    board.declare(build_entry("pc-a::GPU0", job="唯一の宣言"))
    path, entry = board.pairs_for("pc-a::GPU0")[0]
    assert (
        board._remove_if_nonce("pc-a::GPU0", expect_nonce=entry.nonce, reason="事前に消す")
        is RemovalResult.REMOVED
    )
    # 別の宣言をもう 1 件置く。これが読めなくなる。
    board.declare(build_entry("pc-a::GPU0", job="読めなくなる宣言"))

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("共有違反")

    monkeypatch.setattr(Path, "read_text", boom)
    try:
        result = board._remove_if_nonce(
            "pc-a::GPU0", expect_nonce=entry.nonce, reason="テスト", known=(path, entry)
        )
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)

    assert result is RemovalResult.UNCONFIRMED, f"確認できないのに {result} と断定した"
