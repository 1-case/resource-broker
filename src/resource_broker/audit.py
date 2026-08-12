"""監査ログの追記。掲示板とレジストリの両方から使う。

「沈黙は成功ではない」（CLAUDE.md）を守るための記録である。本ツールは fail-open のため
**握りつぶした失敗が表に出ない**。後から「なぜそう判定したか」「いつ判定が止まったか」を
追える唯一の手段がここになる。

追記に失敗しても呼び出し側を止めない。監査の失敗で本処理が落ちるのでは本末転倒である。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import clock

#: 1 行の上限。これを超えると 1 回の write で書き切れず、他プロセスと交錯しうる。
#: 自由記述の ``observed.note`` が長くなりうるため上限を設ける。
MAX_LINE_CHARS = 8000


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

        line = json.dumps(record, ensure_ascii=False, default=str)
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…[truncated]"
        data = (line + "\n").encode("utf-8", errors="replace")

        # マシン上の全セッションが同じファイルへ追記する。テキスト層のバッファ越しに書くと、
        # 長い行がバッファ境界で分割されて他プロセスの行と交錯しうる。O_APPEND + 1 回の
        # write なら、行の途中に割り込まれない（「沈黙は成功ではない」の唯一の担保を守る）。
        handle = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
    except Exception:  # noqa: BLE001 - 監査の失敗で本処理を止めない
        return
