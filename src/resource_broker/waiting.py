"""資源が解放されるまで待つ。

**待つのはコマンドの中だけである。** フックの中では絶対に待たない。フックでブロックすると
セッションが固まり、ユーザーは Esc でも抜けられず、画面上は何も起きていないように見える
（CLAUDE.md「Enforcement vs Waiting」）。``rb wait`` はツール呼び出しとして可視であり、
Ctrl+C で中断できる。

**毎回のポーリングを監査ログに残す。** 「通知が来ない」は「まだ使用中」と「監視が死んだ」を
区別できない。過去に、監視プロセスが再起動で死んだまま 6 時間 45 分にわたり資源が空いて
いたことに誰も気づかなかった事故がある。記録が残っていれば、最終ポーリング時刻を見て
「監視が死んでいる」と判断できる。

**ETA では打ち切らない。** 掲示板の ETA は申告であって約束ではない。過ぎたからといって
待機をやめる根拠にはしない（CLAUDE.md「Time Handling」）。打ち切るのは呼び出し側が
明示した ``timeout`` だけである。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import audit, clock
from .board import Board, Entry

#: ポーリングの既定間隔（秒）。
DEFAULT_INTERVAL_S = 10.0

#: 待機の既定上限（秒）。無限に待たない。
#:
#: 待ち続けたまま戻らないと、セッションから見て「固まった」と区別できない。
#: 上限に達したら一度戻し、続けるかどうかを呼び出し側に決めさせる。
DEFAULT_TIMEOUT_S = 3600.0


@dataclass(frozen=True)
class WaitResult:
    """待機の結果。

    Attributes
    ----------
    released : bool
        解放されたか。タイムアウトなら False。
    polls : int
        ポーリングした回数。
    waited_s : float
        待った秒数。
    last : Entry or None
        最後に見えた宣言。解放されていれば None。
    """

    released: bool
    polls: int
    waited_s: float
    last: Entry | None


def wait_for_release(
    board: Board,
    resource_id: str,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = clock.now,
) -> WaitResult:
    """掲示板から宣言が消えるまで待つ。

    Parameters
    ----------
    board : Board
        掲示板。
    resource_id : str
        正規化済みの資源 ID。
    interval_s : float
        ポーリング間隔。
    timeout_s : float
        待機の上限。超えたら ``released=False`` で戻る。
    sleep : callable
        待機の実装。テストで差し替える（実時間を待たないため）。
    now : callable
        現在時刻の取得。テストで差し替える。

    Returns
    -------
    WaitResult
        解放されたかどうかと、待った回数・秒数。

    Notes
    -----
    掲示板が読めないときは「宣言が無い」とみなして解放扱いにする。
    インフラの故障で永久に待たせるより通すほうがよい（fail-open）。
    """
    started = now()
    polls = 0

    while True:
        entry = board.read(resource_id)
        polls += 1
        elapsed = (now() - started).total_seconds()
        audit.append(
            board.root,
            "wait_poll",
            resource=resource_id,
            held=entry is not None,
            polls=polls,
            elapsed_s=round(elapsed, 3),
        )

        if entry is None:
            audit.append(board.root, "wait_released", resource=resource_id, polls=polls)
            return WaitResult(released=True, polls=polls, waited_s=elapsed, last=None)

        if elapsed >= timeout_s:
            audit.append(
                board.root, "wait_timeout", resource=resource_id, polls=polls, elapsed_s=elapsed
            )
            return WaitResult(released=False, polls=polls, waited_s=elapsed, last=entry)

        sleep(min(interval_s, max(0.0, timeout_s - elapsed)))
