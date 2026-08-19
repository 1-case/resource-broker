"""資源の状況が変わるまで待つ。

起きるのは**宣言の集合が縮んだとき**である。誰かが解放すれば縮み、相乗りが増えれば増える。
資源が空く方向に動いたときだけ起こしたいので、**縮んだときだけ**戻る。

「どれくらい減ったか」は判定しない。使用量の単位も尺度も資源ごとに違い、それを解釈すれば
その資源を知ることになる（CLAUDE.md「Resource Agnosticism」）。**増減だけを見れば方向は分かる**
ので、数値を読む必要がない。入れるかどうかは起きた側が自分で調べて決める。駄目ならまた待てばよい。

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
from .board import Board, Entry, first_declaration

#: ポーリングの既定間隔（秒）。
DEFAULT_INTERVAL_S = 10.0

#: 待機の既定上限（秒）。無限に待たない。
#:
#: 待ち続けたまま戻らないと、セッションから見て「固まった」と区別できない。
#: 上限に達したら一度戻し、続けるかどうかを呼び出し側に決めさせる。
DEFAULT_TIMEOUT_S = 3600.0


#: 待機が終わった理由。
RELEASED = "released"
"""宣言が 1 つも無くなった。"""

SHRANK = "shrank"
"""宣言の数が減った（誰かが解放した）。まだ他の宣言は残っている。"""

TIMEOUT = "timeout"


@dataclass(frozen=True)
class WaitResult:
    """待機の結果。

    Attributes
    ----------
    reason : str
        終わった理由。``released`` / ``changed`` / ``timeout``。
    polls : int
        ポーリングした回数。
    waited_s : float
        待った秒数。
    last : Entry or None
        最後に見えた宣言。解放されていれば None。
    """

    reason: str
    polls: int
    waited_s: float
    last: Entry | None
    holders: int = 0

    @property
    def released(self) -> bool:
        """宣言が 1 つも無くなったか。"""
        return self.reason == RELEASED

    @property
    def worth_checking(self) -> bool:
        """もう一度自分で調べる価値があるか。

        全部無くなっても、1 つ減っただけでも「調べ直せ」であり、扱いは同じである。
        """
        return self.reason in (RELEASED, SHRANK)


def holder_keys(board: Board, resource_id: str) -> set[str]:
    """その資源を宣言している者の集合を返す。**宣言は全て対等に数える。**

    **中身は見ない。** 誰が何人いるかだけを数える。増減が分かれば資源が空く方向に
    動いたかは判断でき、使用量の数値を解釈する必要がない。

    キーには **nonce**（宣言ごとに一意）を使う。``since`` と ``session`` で作ると、
    宣言者が別セッションへ**交代しただけ**で「1 人消えた」に見える。件数は変わって
    いないのに ``rb wait`` が戻り、待っている側は入れないまま起こされる。
    nonce を持たない古い宣言だけ従来のキーで代替する。
    """
    keys: set[str] = set()
    for entry in board.list_for(resource_id):
        keys.add(entry.nonce or f"{entry.since}:{entry.session}")
    return keys


def wait_for_room(
    board: Board,
    resource_id: str,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = clock.now,
) -> WaitResult:
    """宣言している者が減るまで待つ。

    完全解放だけを待つと、相乗りできる資源で機会を逃す。逆に、相乗りが**増えた**ときに
    起こしても意味がない（資源はさらに詰まっている）。したがって**減ったときだけ**戻る。

    Parameters
    ----------
    board : Board
        掲示板。
    resource_id : str
        正規化済みの資源 ID。
    interval_s : float
        ポーリング間隔。
    timeout_s : float
        待機の上限。超えたら ``reason="timeout"`` で戻る。
    sleep : callable
        待機の実装。テストで差し替える（実時間を待たないため）。
    now : callable
        現在時刻の取得。テストで差し替える。

    Returns
    -------
    WaitResult
        終わった理由と、待った回数・秒数。

    Notes
    -----
    掲示板が読めないときは「宣言が無い」とみなして解放扱いにする。
    インフラの故障で永久に待たせるより通すほうがよい（fail-open）。
    """
    started = now()
    polls = 0
    baseline: set[str] | None = None

    while True:
        keys = holder_keys(board, resource_id)
        polls += 1
        elapsed = (now() - started).total_seconds()
        if baseline is None:
            baseline = set(keys)

        gone = baseline - keys
        audit.append(
            board.root,
            "wait_poll",
            resource=resource_id,
            holders=len(keys),
            gone=len(gone),
            polls=polls,
            elapsed_s=round(elapsed, 3),
        )

        if not keys:
            audit.append(board.root, "wait_released", resource=resource_id, polls=polls)
            return WaitResult(reason=RELEASED, polls=polls, waited_s=elapsed, last=None, holders=0)

        # **件数が減ったときだけ戻る。** 「誰かが消えた」だけでは足りない。
        # 保持者が交代した（1 人抜けて 1 人入った）場合、資源は空いていないのに
        # `gone` は非空になる。空く方向に動いたかは件数でしか分からない。
        if gone and len(keys) < len(baseline):
            audit.append(
                board.root, "wait_shrank", resource=resource_id, polls=polls, gone=len(gone)
            )
            return WaitResult(
                reason=SHRANK,
                polls=polls,
                waited_s=elapsed,
                last=first_declaration(board, resource_id),
                holders=len(keys),
            )

        # 起こさなかった分は基準に取り込む。増加でも交代でも起こさないが、
        # **そのあとの減少は検知したい**。和を取ると交代のたびに基準が膨らみ、
        # 次のポーリングで「減った」に化けるので、現在の集合で置き換える。
        baseline = set(keys)

        if elapsed >= timeout_s:
            audit.append(
                board.root, "wait_timeout", resource=resource_id, polls=polls, elapsed_s=elapsed
            )
            return WaitResult(
                reason=TIMEOUT,
                polls=polls,
                waited_s=elapsed,
                last=first_declaration(board, resource_id),
                holders=len(keys),
            )

        sleep(min(interval_s, max(0.0, timeout_s - elapsed)))
