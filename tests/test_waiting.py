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
from datetime import datetime, timedelta
from pathlib import Path

from resource_broker import clock, waiting
from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

RESOURCE = normalize("GPU0")


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
    assert board.try_claim(build_entry(RESOURCE, job="E059 eval", session="folnet", eta=eta))


def join(board: Board, cwd: str) -> None:
    """相乗りを置く。"""
    entry = build_entry(RESOURCE, job="相乗りのジョブ", cwd=cwd, session="malm")
    assert board.add_join(entry, cwd)


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
            board.remove(RESOURCE, reason="テストで解放")

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
    join(board, "C:\\works\\malm")
    fake = FakeClock()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        fake.sleep(seconds)
        if calls["n"] == 2:
            board.remove_join(RESOURCE, "C:\\works\\malm", reason="テストで離脱")

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
            join(board, "C:\\works\\malm")

    result = waiting.wait_for_room(
        board, RESOURCE, interval_s=10, timeout_s=60, sleep=sleep, now=fake.now
    )

    assert result.reason == waiting.TIMEOUT  # 増えても起きない
    assert result.holders == 2


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
            join(board, "C:\\works\\malm")
        if calls["n"] == 3:
            board.remove_join(RESOURCE, "C:\\works\\malm", reason="テストで離脱")

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
