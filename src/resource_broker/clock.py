"""時刻生成の唯一の入口。

掲示板に書く時刻は**すべてここを通す**。LLM に時刻を書かせない・推定させないための
単一窓口である。出力は必ず ISO 8601 + オフセット付きで、
JST と UTC の取り違えが起きない形にする。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

#: 期間の表記。``30m`` ``2h`` ``90s`` ``1d`` と、それらの連結（``1h30m``）を受ける。
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def now() -> datetime:
    """現在時刻をローカルタイムゾーン付きで返す。

    Returns
    -------
    datetime
        タイムゾーン情報（オフセット）を持つ現在時刻。
    """
    return datetime.now().astimezone()


def to_iso(moment: datetime) -> str:
    """datetime を ISO 8601 + オフセットの文字列にする。

    Parameters
    ----------
    moment : datetime
        変換対象。tz-naive の場合はローカルタイムゾーンを補う。

    Returns
    -------
    str
        例: ``2026-08-12T03:12:15+09:00``
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


def now_iso() -> str:
    """現在時刻を ISO 8601 + オフセットの文字列で返す。"""
    return to_iso(now())


def parse_duration(text: str | None) -> timedelta | None:
    """``30m`` ``2h`` ``1h30m`` のような期間表記を timedelta にする。

    ETA の申告を「機械が計算した絶対時刻」に変換するために使う。**申告するのは
    セッション（LLM）だが、時刻の計算は機械が行う**。LLM に「終了予定は 03:40」と
    書かせると、JST と UTC の取り違えや単純な足し算の誤りが起きる（実際に起きた）。

    解釈できなければ None を返す。自由記述の ETA（「モデル次第」等）を許すためであり、
    解釈できないことは異常ではない。

    Parameters
    ----------
    text : str or None
        期間表記。

    Returns
    -------
    timedelta or None
        解釈できた場合は timedelta、できなければ None。
    """
    if not text:
        return None
    # 負の期間は受けない。`-5m` から `5m` を拾って「5 分後」と解釈すると、
    # 書いた側の意図と正反対の時刻が掲示板に載る。
    if re.search(r"-\s*\d", text):
        return None
    matches = _DURATION.findall(text)
    if not matches:
        return None
    seconds = 0.0
    for amount, unit in matches:
        try:
            seconds += float(amount) * _UNIT_SECONDS[unit.lower()]
        except (TypeError, ValueError, KeyError):
            return None
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def parse_iso(text: str | None) -> datetime | None:
    """ISO 8601 文字列を datetime にする。解釈できなければ None を返す。

    掲示板は他プロセスが書いたファイルを読むため、壊れた値が来ることを前提にする。
    例外を投げずに None を返すことで、呼び出し側の fail-open を単純にする。

    Parameters
    ----------
    text : str or None
        ISO 8601 文字列。

    Returns
    -------
    datetime or None
        解釈できた場合はタイムゾーン付き datetime、できなければ None。
    """
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed
