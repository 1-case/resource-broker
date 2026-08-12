"""時刻生成の唯一の入口。

掲示板に書く時刻は**すべてここを通す**。LLM に時刻を書かせない・推定させないための
単一窓口である（CLAUDE.md「Time Handling」）。出力は必ず ISO 8601 + オフセット付きで、
JST と UTC の取り違えが起きない形にする。
"""

from __future__ import annotations

from datetime import datetime


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
