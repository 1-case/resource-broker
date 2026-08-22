"""``rb wait`` の守護テスト。

待つのはコマンドの中だけである。フックの中では待たない（ブロックするとセッションが固まり、
Esc でも抜けられない）。ここで守るのは 4 つ。

1. 宣言している者が**減った**ら戻る（解放も、相乗りの離脱も）
2. **増えたときには戻らない**。資源はさらに詰まっているので起こす意味がない
3. **ETA では打ち切らない**（申告であって約束ではない）
4. **毎回のポーリングを監査ログに残す**（「まだ使用中」と「監視が死んだ」を区別できるように）

4 が特に重い。過去に、監視プロセスが再起動で死んだまま 6 時間 45 分にわたり資源が空いて
いたことに誰も気づかなかった事故がある。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from resource_broker import clock, waiting
from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize


def _joined_nonce(board: Board, cwd: str) -> str:
    """その場所から出された宣言の nonce。

    平坦化で宣言は対等になったので、``cwd`` だけで消すと**祖先関係で他の宣言まで
    巻き込む**。テストが狙った 1 件だけを外すために nonce で指す。
    """
    for entry in board.list_for(RESOURCE):
        if str(entry.holder.get("cwd") or "") == cwd:
            return entry.nonce
    raise AssertionError(f"{cwd} から出された宣言が無い")


RESOURCE = normalize("GPU0")

#: 相乗り者の作業ディレクトリ。**ネイティブの区切りで組み立てる。**
#:
#: Windows 形式のリテラルを実パスとして使うと、POSIX では ``\`` が区切りではないため
#: 配下判定が意味を失う（テストは通るが何も検証しなくなる）。
JOINER_CWD = os.path.join(os.sep, "works", "malm")


class FakeClock:
    """呼ばれるたびに進む時計。実時間を待たずに経過を作る。"""

    def __init__(self) -> None:
        self.moment = clock.now()

    def now(self) -> datetime:
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def declare(board: Board, *, eta: str = "40m") -> None:
    """主宣言を置く。"""
    assert board.declare(build_entry(RESOURCE, job="E059 eval", session="folnet", eta=eta))


def join(board: Board, cwd: str) -> None:
    """相乗りを置く。"""
    entry = build_entry(RESOURCE, job="相乗りのジョブ", cwd=cwd, session="malm")
    assert board.declare(entry)


def audit_events(root: Path) -> list[dict[str, object]]:
    records = []
    for path in sorted((root / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return records


def wait(board: Board, fake: FakeClock, **kwargs: object) -> waiting.WaitResult:
    return waiting.wait_for_room(
        board,
        RESOURCE,
        sleep=fake.sleep,
        now=fake.now,
        **kwargs,  # type: ignore[arg-type]
    )


# --- 減ったら戻る ---------------------------------------------------------------


def test_returns_immediately_when_nobody_holds_it(tmp_path: Path) -> None:
    """誰も宣言していなければ即座に戻る。"""
    result = wait(Board(tmp_path), FakeClock())

    assert result.reason == waiting.RELEASED
    assert result.polls == 1


def test_returns_when_the_primary_is_released(tmp_path: Path) -> None:
    """主宣言が解放されたら戻る。"""
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 3:
            board.remove_all(RESOURCE, reason="テストで解放")

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=5, timeout_s=1000, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.RELEASED
    assert result.polls == 4


def test_returns_when_a_joiner_leaves(tmp_path: Path) -> None:
    """相乗りが 1 人抜けても戻る。資源が空く方向に動いたからである。

    完全解放だけを待つと、相乗りできる資源で機会を逃す。
    """
    board = Board(tmp_path)
    declare(board)
    join(board, JOINER_CWD)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 2:
            board.remove_own(
                RESOURCE,
                cwd=JOINER_CWD,
                reason="テストで離脱",
                nonce=_joined_nonce(board, JOINER_CWD),
            )

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=5, timeout_s=1000, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.SHRANK
    assert result.holders == 1  # 主宣言は残っている
    assert result.last is not None


# --- 増えたときは戻らない -------------------------------------------------------


def test_does_not_return_when_a_joiner_is_added(tmp_path: Path) -> None:
    """相乗りが**増えた**ときには戻らない。

    資源はさらに詰まっているので、起こしても入れない。
    """
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 2:
            join(board, JOINER_CWD)

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=10, timeout_s=60, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.TIMEOUT  # 増えても起きない
    assert result.holders == 2


def test_holder_replacement_is_not_a_shrink(tmp_path: Path) -> None:
    """保持者が**交代**しただけでは戻らない。

    主宣言が別セッションへ渡っても件数は変わっていない。「誰かが消えた」だけで
    戻ると、待っている側は入れないまま起こされる。キーに nonce を使い、
    かつ件数が減ったことを条件にする。
    """
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 2:
            board.remove_all(RESOURCE, reason="テストで交代")
            assert board.declare(build_entry(RESOURCE, job="別のジョブ", session="malm"))

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=5, timeout_s=40, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.TIMEOUT
    assert result.holders == 1


def test_shrink_after_a_replacement_still_wakes(tmp_path: Path) -> None:
    """交代のあとに解放されたら戻る。

    交代を「減った」と誤認しないための基準の入れ替えが、そのあとの減少を
    取りこぼす方向に効いてはならない。
    """
    board = Board(tmp_path)
    declare(board)
    join(board, JOINER_CWD)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 1:
            board.remove_all(RESOURCE, reason="テストで交代")
            assert board.declare(build_entry(RESOURCE, job="別のジョブ", session="malm"))
        if calls["n"] == 3:
            board.remove_own(
                RESOURCE,
                cwd=JOINER_CWD,
                reason="テストで離脱",
                nonce=_joined_nonce(board, JOINER_CWD),
            )

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=5, timeout_s=1000, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.SHRANK
    assert result.holders == 1


def test_growth_then_shrink_still_wakes(tmp_path: Path) -> None:
    """増えたあとに減れば戻る。

    増加を基準に取り込むので、そのあとの減少を取りこぼさない。
    """
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 1:
            join(board, JOINER_CWD)
        if calls["n"] == 3:
            board.remove_own(
                RESOURCE,
                cwd=JOINER_CWD,
                reason="テストで離脱",
                nonce=_joined_nonce(board, JOINER_CWD),
            )

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=5, timeout_s=1000, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.SHRANK


# --- ETA と上限 -----------------------------------------------------------------


def test_eta_does_not_end_the_wait(tmp_path: Path) -> None:
    """ETA を過ぎても待機をやめない。

    掲示板の ETA は申告であって約束ではない。過ぎたからといって「終わったはず」と
    みなすのは、まさに ETA を判断に使うことである（CLAUDE.md「Time Handling」）。
    """
    board = Board(tmp_path)
    declare(board, eta="1s")
    fake = FakeClock()

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=10, timeout_s=60, sleep=fake.sleep, now=fake.now
    )

    assert result.reason == waiting.TIMEOUT


def test_timeout_returns_instead_of_waiting_forever(tmp_path: Path) -> None:
    """上限に達したら一度戻る。

    待ち続けたまま戻らないと、セッションから見て「固まった」と区別できない。
    """
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=10, timeout_s=30, sleep=fake.sleep, now=fake.now
    )

    assert result.reason == waiting.TIMEOUT
    assert result.waited_s >= 30


# --- 監査ログ -------------------------------------------------------------------


def test_every_poll_is_audited(tmp_path: Path) -> None:
    """毎回のポーリングを監査ログに残す。

    「通知が来ない」は「まだ使用中」と「監視が死んだ」を区別できない。
    記録があれば、最終ポーリング時刻から死亡を判断できる。
    """
    board = Board(tmp_path)
    declare(board)
    fake = FakeClock()

    waiting.wait_for_room(
        board, RESOURCE, interval_s=10, timeout_s=30, sleep=fake.sleep, now=fake.now
    )

    events = audit_events(tmp_path)
    polls = [r for r in events if r.get("event") == "wait_poll"]
    assert len(polls) >= 3
    assert all(r.get("resource") == RESOURCE for r in polls)
    assert any(r.get("event") == "wait_timeout" for r in events)


def test_release_is_audited(tmp_path: Path) -> None:
    """解放を検知したことも残す。"""
    waiting.wait_for_room(Board(tmp_path), RESOURCE, sleep=FakeClock().sleep, now=FakeClock().now)

    assert any(r.get("event") == "wait_released" for r in audit_events(tmp_path))


# --- fail-open ------------------------------------------------------------------


def test_unreadable_board_is_treated_as_released(tmp_path: Path) -> None:
    """掲示板が読めないときは通す。

    インフラの故障で永久に待たせるより、通すほうがよい（fail-open）。
    """
    result = wait(Board(tmp_path / "存在しない"), FakeClock())

    assert result.reason == waiting.RELEASED


# --- 待っている側に逃げ道を示す ---------------------------------------------------


@pytest.fixture(autouse=True)
def _boot_is_long_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    """起動時刻を十分に過去へ固定する。

    **テストが開発機の稼働時間に依存していた。** `hold_gpu(minutes_ago=200)` は
    3h20m 前の宣言を作るが、起動から間もないマシン（CI のランナーはまさにそれ）では
    それが**本物の再起動またぎ**になり、`holder_keys` が確定的な幽霊として数えなく
    なる。開発機では稼働時間が長いので通り、CI で初めて落ちた。

    再起動またぎを試したいテストは、この固定を自分で上書きする。
    """
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() - timedelta(days=30)
    )


def hold_gpu(tmp_path: Path, *, minutes_ago: int = 0) -> None:
    """他セッションの宣言を 1 件置く。``minutes_ago`` で古さを作る。"""
    board = Board(tmp_path)
    entry = build_entry(
        normalize("GPU0"),
        job="E061 本計測",
        session="folnet",
        cwd=str(tmp_path / "other-session"),
        eta="9h",
    )
    if minutes_ago:
        entry.since = (
            datetime.fromisoformat(entry.since) - timedelta(minutes=minutes_ago)
        ).isoformat()
    assert board.declare(entry)


def test_wait_tells_the_waiter_how_to_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """待機の開始時に「自分で調べて、駄目なら人間へ」を伝える。

    本ツールは実測が空きでも宣言を退けない。だから掲示板が古いまま固まると、
    抜ける道は保持者か人間しかない。それを黙っていると待ち続けるしかなくなる。
    実際に解放し忘れで 2 時間 48 分待たせた。
    """
    hold_gpu(tmp_path)
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "wait", "GPU0", "--timeout", "0"]) != 0
    captured = capsys.readouterr()
    text = captured.out + captured.err

    assert "自分でも資源の状態を調べる" in text
    assert "人間に相談" in text


def test_wait_shows_how_long_the_holder_has_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """保持者がどれだけ持っているかを出す。

    ``since`` の生の時刻だけでは古さが分からない（人間が引き算するしかない）。
    **これは表示であって判断ではない。** 長く持っていることは幽霊の証拠にならない。
    """
    hold_gpu(tmp_path, minutes_ago=200)
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "wait", "GPU0", "--timeout", "0"]) != 0
    captured = capsys.readouterr()

    assert "3h20m 経過" in captured.out


def test_wait_timeout_names_the_holder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """上限で戻るときに保持者と助言を出す。

    ここで黙ると、待っている側は同じ待機を繰り返すしかない。
    """
    hold_gpu(tmp_path, minutes_ago=200)
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "wait", "GPU0", "--timeout", "0"]) != 0
    err = capsys.readouterr().err

    assert "保持者: folnet" in err
    assert "3h20m 前から" in err
    assert "人間に相談" in err


def test_status_shows_the_elapsed_time(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``rb status`` にも経過時間を出す。古さが一目で分かるようにする。"""
    hold_gpu(tmp_path, minutes_ago=452)
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "status"]) == 0

    assert "7h32m 経過" in capsys.readouterr().out


def test_an_internal_error_does_not_look_like_a_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rb wait`` の内部エラーを 0（＝宣言が減った）に化けさせない。

    wait の 0 は「全宣言が消えた／減った」という**積極的な意味**を持つ。内部エラーで
    0 を返すと、1 度も待っていないのに「空いた」と読まれ、使用中の資源を掴みにいく。
    fail-open は「情報が無いなら通す」であって「嘘をつく」ではない。
    """
    hold_gpu(tmp_path)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("待機の内部が壊れた")

    monkeypatch.setattr(waiting, "wait_for_room", explode)

    assert main(["--home", str(tmp_path), "wait", "GPU0"]) != 0


def test_a_broken_wait_is_distinguishable_from_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部エラーと上限到達を同じ終了コードにしない。

    どちらも 1 にすると、呼び出し側が「上限まで待った」と「1 度も待っていない」を
    区別できない。**前者は待ち直す価値があり、後者は原因を調べる必要がある。**
    対処が違うものを畳まない。
    """
    from resource_broker import cli

    hold_gpu(tmp_path)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("待機の内部が壊れた")

    monkeypatch.setattr(waiting, "wait_for_room", explode)
    broken = main(["--home", str(tmp_path), "wait", "GPU0"])

    monkeypatch.undo()
    hold_gpu(tmp_path) if not Board(tmp_path).list_for(RESOURCE) else None
    timed_out = main(["--home", str(tmp_path), "wait", "GPU0", "--timeout", "0"])

    assert broken == cli.EXIT_BROKEN
    assert timed_out == cli.EXIT_BUSY
    assert broken != timed_out


def test_exit_broken_keeps_its_original_value_after_the_rename() -> None:
    """``EXIT_WAIT_BROKEN`` → ``EXIT_BROKEN`` への改名は**値を変えていない**。

    値 3 は ``wait`` 専用だった頃から外部（呼び出し側のシェルスクリプト等）が
    見ている可能性がある。カテゴリを一般化する（``release --nonce`` にも使う）
    のは値の意味を広げるだけであり、既存の値そのものを変えてはならない。
    シンボル名だけを比較するテストは、両方が一緒に動いてしまえば検出力を
    持たない——ここでは文字どおりの値を固定する。
    """
    from resource_broker import cli

    assert cli.EXIT_BROKEN == 3
    assert cli.EXIT_BROKEN not in (
        cli.EXIT_OK,
        cli.EXIT_BUSY,
        cli.EXIT_USAGE,
        cli.EXIT_INTERRUPTED,
    )


def test_a_reboot_ghost_does_not_keep_wait_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再起動をまたいだ宣言だけの資源では、``rb wait`` はすぐ戻る。

    確定的な幽霊なので `rb claim` は即座に退けて取れる。ここで数えてしまうと、
    **同じ掲示板を見て 2 つのコマンドが逆のことを言う**——`claim` は「取れる」、
    `wait` は上限まで待って「まだ使用中です」。
    """
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="落ちたセッション", session="theirs")
    entry.since = clock.to_iso(clock.now() - timedelta(hours=3))
    assert board.declare(entry)

    # 既定の固定（起動は 30 日前）では、この宣言は起動より後なので数える
    assert waiting.holder_keys(board, RESOURCE) != set()

    # 起動がこの宣言より後 = 再起動をまたいでいる
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() - timedelta(hours=1)
    )

    assert waiting.holder_keys(board, RESOURCE) == set(), "確定的な幽霊を数えている"


def test_a_release_and_a_shrink_both_exit_zero_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 越しに RELEASED と SHRANK がどちらも 0 で戻る。

    **本体（``wait_for_room``）を見るテストでは、この保証は取れない。** 終了コードへの
    対応付けは ``_cmd_wait`` の分岐にあり、片方の ``return`` を書き換えても本体側の
    テストは全部通ってしまう。

    「全部消えた」も「1 つ減った」も待機としては成功で、呼び出し側への合図は同じ
    **「もう一度自分で調べろ」**である。上限到達（``EXIT_BUSY``）や内部エラー
    （``EXIT_BROKEN``）と畳まない——対処が違う。
    """
    from resource_broker import cli

    hold_gpu(tmp_path)

    def finished(reason: str):  # type: ignore[no-untyped-def]
        def stub(*_args: object, **_kwargs: object) -> waiting.WaitResult:
            return waiting.WaitResult(reason=reason, polls=1, waited_s=0.0, last=None, holders=1)

        return stub

    monkeypatch.setattr(waiting, "wait_for_room", finished(waiting.RELEASED))
    released = main(["--home", str(tmp_path), "wait", "GPU0"])

    monkeypatch.setattr(waiting, "wait_for_room", finished(waiting.SHRANK))
    shrank = main(["--home", str(tmp_path), "wait", "GPU0"])

    assert released == cli.EXIT_OK
    assert shrank == cli.EXIT_OK, "宣言が減っただけの復帰を成功として扱っていない"
    assert cli.EXIT_OK not in (cli.EXIT_BUSY, cli.EXIT_BROKEN)
