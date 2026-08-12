"""監査ログの追記。掲示板とレジストリの両方から使う。

「沈黙は成功ではない」（CLAUDE.md）を守るための記録である。本ツールは fail-open のため
**握りつぶした失敗が表に出ない**。後から「なぜそう判定したか」「いつ判定が止まったか」を
追える唯一の手段がここになる。

追記に失敗しても呼び出し側を止めない。監査の失敗で本処理が落ちるのでは本末転倒である。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import clock


def audit_dir(root: Path) -> Path:
    """監査ログを置くディレクトリ。"""
    return root / "audit"


def append(root: Path, event: str, **fields: object) -> None:
    """監査ログに 1 行追記する。失敗しても黙って諦める。

    Parameters
    ----------
    root : Path
        掲示板のルート。この直下の ``audit/`` へ日付ごとの JSONL を作る。
    event : str
        イベント名。``claimed`` ``entry_corrupt`` など。
    **fields
        イベントに付随する情報。JSON にできない値は文字列化する。
    """
    record = {"at": clock.now_iso(), "event": event, **fields}
    try:
        directory = audit_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{clock.now().strftime('%Y-%m-%d')}.jsonl"
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - 監査の失敗で本処理を止めない
        return
