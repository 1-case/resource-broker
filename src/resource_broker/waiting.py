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

from . import audit, clock, liveness, platform_info
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
"""上限まで待った。**少なくとも 1 回は掲示板を完全に読めている**——だから
「正常に読めた上でまだ使用中」と言い切れる。"""

BROKEN = "broken"
"""掲示板が**一度も完全に読めないまま**上限に達した。

``TIMEOUT`` と畳んではならない。``TIMEOUT`` は「確認した上でまだ使用中」だが、
こちらは確認そのものが 1 度も取れていない——「使用中」と「読めない」を混同すると、
読めない掲示板を待ち続けた末に、確認していない使用中を報告することになる
（issue #17 指摘 4）。"""


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

    **完全性は捨てる。** 読めなかったものがあっても黙って飛ばす。``rb wait`` の
    ように「空集合を積極的な成功（解放済み）と読む」場面では、必ず
    :func:`holder_keys_detailed` を使うこと——読めない掲示板を「宣言が無い」に
    畳むと、実際には使用中の資源を「解放済み」と答えてしまう（issue #17 指摘 4）。
    """
    return holder_keys_detailed(board, resource_id)[0]


def holder_keys_detailed(board: Board, resource_id: str) -> tuple[set[str], bool]:
    """:func:`holder_keys` と同じだが、**掲示板を完全に読めたか**も返す。

    **中身は見ない。** 誰が何人いるかだけを数える。増減が分かれば資源が空く方向に
    動いたかは判断でき、使用量の数値を解釈する必要がない。

    キーには **nonce**（宣言ごとに一意）を使う。``since`` と ``session`` で作ると、
    宣言者が別セッションへ**交代しただけ**で「1 人消えた」に見える。件数は変わって
    いないのに ``rb wait`` が戻り、待っている側は入れないまま起こされる。
    nonce を持たない古い宣言だけ従来のキーで代替する。

    Returns
    -------
    tuple of (set of str, bool)
        キーの集合と、**掲示板を完全に読めたか**。``False`` のとき、返した集合は
        過小である可能性がある——読めなかった側に宣言が隠れているかもしれない。
    """
    listing = board.pairs_for_detailed(resource_id)
    boot = platform_info.boot_time()
    keys: set[str] = set()
    for _, entry in listing.pairs:
        # **再起動をまたいだ宣言は数えない。** 確定的な幽霊であり、`rb claim` なら
        # 即座に退けて取れる。ここで数えると `rb wait` だけが上限まで待ち切って
        # 「まだ使用中です」と答える——同じ掲示板を見て 2 つのコマンドが逆のことを言う。
        #
        # **`STALE_PROBE` は数える。** あちらは実測（`--found free`）が要るので、
        # 待っている側が単独で判定してよい根拠ではない。
        since = entry.since_dt
        if boot is not None and since is not None and since < boot - liveness.BOOT_MARGIN:
            continue
        keys.add(entry.nonce or f"{entry.since}:{entry.session}")
    return keys, listing.complete


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
    **掲示板が読めないことを「宣言が無い」に畳まない。** 以前はここで畳んでいた
    ——インフラの故障で永久に待たせるより通すほうがよい、という判断自体は
    正しいが、「通す」の中身が「解放済みという積極的な成功表現を返す」になって
    いたのが誤りだった。読めない・部分的にしか読めない場合は**このポーリングを
    無かったことにして次へ回す**（起こさない。件数が減った証拠を持たないため）。
    **一度も完全に読めないまま上限に達したときだけ** ``BROKEN`` で区別する
    （issue #17 指摘 4）。fail-open は「待ち続ける」側であって「解放したと嘘を
    つく」側ではない。
    """
    started = now()
    polls = 0
    baseline: set[str] | None = None
    confirmed = False  # 一度でも掲示板を完全に読めたか

    while True:
        keys, complete = holder_keys_detailed(board, resource_id)
        polls += 1
        elapsed = (now() - started).total_seconds()

        if not complete:
            # **読めない・部分的にしか読めないポーリングは「変化なし」として扱う。**
            # ここで空集合を信じて RELEASED を返すと、読めなかった側に生きた宣言が
            # 隠れているかもしれないのに「解放済み」と積極的に言うことになる
            # ——このツールが最も避けるべき「使用中を空きと言う」誤りである。
            audit.append(
                board.root,
                "wait_unconfirmed",
                resource=resource_id,
                polls=polls,
                elapsed_s=round(elapsed, 3),
            )
            if elapsed >= timeout_s:
                reason = TIMEOUT if confirmed else BROKEN
                return WaitResult(
                    reason=reason,
                    polls=polls,
                    waited_s=elapsed,
                    last=first_declaration(board, resource_id),
                    holders=len(keys),
                )
            sleep(min(interval_s, max(0.0, timeout_s - elapsed)))
            continue

        confirmed = True
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
