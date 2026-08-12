"""``rb wait`` の守護テスト。

待つのはコマンドの中だけである。フックの中では待たない（ブロックするとセッションが固まり、
Esc でも抜けられない）。ここで守るのは 3 つ。

1. 解放されたら戻る
2. **ETA では打ち切らない**（申告であって約束ではない）
3. **毎回のポーリングを監査ログに残す**（「まだ使用中」と「監視が死んだ」を区別できるように）

3 が特に重い。過去に、監視プロセスが再起動で死んだまま 6 時間 45 分にわたり資源が空いて
いたことに誰も気づかなかった事故がある。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from resource_broker import waiting
from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

RESOURCE = normalize("GPU0")


class FakeClock:
    """呼ばれるたびに進む時計。実時間を待たずに経過を作る。"""

    def __init__(self, step_s: float = 1.0) -> None:
        self.moment = build_entry("x", job="y").since_dt or None
        from resource_broker import clock

        self.moment = clock.now()
        self.step_s = step_s

    def now(self):  # noqa: ANN201 - datetime
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def declare(board: Board, *, eta: str = "40m") -> None:
    assert board.try_claim(build_entry(RESOURCE, job="E059 eval", session="folnet", eta=eta))


def audit_events(root: Path) -> list[dict[str, object]]:
    records = []
    for path in sorted((root / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return records


def test_returns_immediately_when_free(tmp_path: Path) -> None:
    """宣言が無ければ即座に戻る。"""
    board = Board(tmp_path)
    clock = FakeClock()

    result = waiting.wait_for_release(board, RESOURCE, sleep=clock.sleep, now=clock.now)

    assert result.released is True
    assert result.polls == 1


def test_returns_when_released(tmp_path: Path) -> None:
    """解放されたら戻る。"""
    board = Board(tmp_path)
    declare(board)
    clock = FakeClock()

    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        clock.sleep(seconds)
        if calls["n"] == 3:
            board.remove(RESOURCE, reason="テストで解放")

    result = waiting.wait_for_release(
        board, RESOURCE, interval_s=5, timeout_s=1000, sleep=sleep, now=clock.now
    )

    assert result.released is True
    assert result.polls == 4
    assert result.last is None


def test_timeout_returns_instead_of_waiting_forever(tmp_path: Path) -> None:
    """上限に達したら一度戻る。

    待ち続けたまま戻らないと、セッションから見て「固まった」と区別できない。
    """
    board = Board(tmp_path)
    declare(board)
    clock = FakeClock()

    result = waiting.wait_for_release(
        board, RESOURCE, interval_s=10, timeout_s=30, sleep=clock.sleep, now=clock.now
    )

    assert result.released is False
    assert result.waited_s >= 30
    assert result.last is not None


def test_eta_does_not_end_the_wait(tmp_path: Path) -> None:
    """ETA を過ぎても待機をやめない。

    掲示板の ETA は申告であって約束ではない。過ぎたからといって「終わったはず」と
    みなすのは、まさに ETA を判断に使うことである（CLAUDE.md「Time Handling」）。
    """
    board = Board(tmp_path)
    declare(board, eta="1s")  # 即座に過ぎる ETA
    clock = FakeClock()

    result = waiting.wait_for_release(
        board, RESOURCE, interval_s=10, timeout_s=60, sleep=clock.sleep, now=clock.now
    )

    assert result.released is False  # ETA を過ぎても解放扱いにしない


def test_every_poll_is_audited(tmp_path: Path) -> None:
    """毎回のポーリングを監査ログに残す。

    「通知が来ない」は「まだ使用中」と「監視が死んだ」を区別できない。
    記録があれば、最終ポーリング時刻から死亡を判断できる。
    """
    board = Board(tmp_path)
    declare(board)
    clock = FakeClock()

    waiting.wait_for_release(
        board, RESOURCE, interval_s=10, timeout_s=30, sleep=clock.sleep, now=clock.now
    )

    polls = [r for r in audit_events(tmp_path) if r.get("event") == "wait_poll"]
    assert len(polls) >= 3
    assert all(r.get("resource") == RESOURCE for r in polls)
    assert any(r.get("event") == "wait_timeout" for r in audit_events(tmp_path))


def test_release_is_audited(tmp_path: Path) -> None:
    """解放を検知したことも残す。"""
    board = Board(tmp_path)
    clock = FakeClock()

    waiting.wait_for_release(board, RESOURCE, sleep=clock.sleep, now=clock.now)

    assert any(r.get("event") == "wait_released" for r in audit_events(tmp_path))


def test_unreadable_board_is_treated_as_released(tmp_path: Path) -> None:
    """掲示板が読めないときは通す。

    インフラの故障で永久に待たせるより、通すほうがよい（fail-open）。
    """
    board = Board(tmp_path / "存在しない")
    clock = FakeClock()

    result = waiting.wait_for_release(board, RESOURCE, sleep=clock.sleep, now=clock.now)

    assert result.released is True
