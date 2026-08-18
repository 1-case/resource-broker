"""監査ログの追記。掲示板とレジストリの両方から使う。

「沈黙は成功ではない」（CLAUDE.md）を守るための記録である。本ツールは fail-open のため
**握りつぶした失敗が表に出ない**。後から「なぜそう判定したか」「いつ判定が止まったか」を
追える唯一の手段がここになる。

追記に失敗しても呼び出し側を止めない。監査の失敗で本処理が落ちるのでは本末転倒である。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import clock

#: 1 行の上限（**エンコード後のバイト数**）。
#:
#: 文字数で測ってはならない。日本語は UTF-8 で 1 文字 3 バイトになるため、
#: 8000 文字の上限は実際には 24000 バイトを許すことになる。
#: ``O_APPEND`` の write が他プロセスと交錯しないのは、POSIX で ``PIPE_BUF``
#: （多くの環境で 4096 バイト）以下のときだけである。ここはその内側に収める。
MAX_LINE_BYTES = 4000

#: 監査ログを残す日数。
#:
#: **単調増加させない。** ここには ``job`` の自由記述と宣言元の ``cwd`` が入る——
#: つまり利用者がいつ何の作業をしていたかの履歴である。``rb wait`` は 10 秒ごとに
#: 1 行足すので、放っておけば増え続ける。用途は「なぜそう判定したか」を後から辿ること
#: であり、その必要が生じるのはたいてい直後である。長く持つ理由が無い。
#:
#: ログ（7 日）より長いのは、監査が**判定の履歴**であり、事故に気づくまでの間が
#: 長くなりうるためである。
AUDIT_RETENTION_DAYS = 90


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

        # **その日の最初の 1 行を書くときだけ掃除する。** 毎回さらうと、追記 1 回ごとに
        # ディレクトリ全体を走査することになる。日付が変わった瞬間は必ず来るので、
        # 掃除の機会は 1 日 1 回確実に訪れる。
        stale = not target.exists()

        data = _encode(record)

        # マシン上の全セッションが同じファイルへ追記する。テキスト層のバッファ越しに書くと、
        # 長い行がバッファ境界で分割されて他プロセスの行と交錯しうる。O_APPEND + 1 回の
        # write なら、POSIX では PIPE_BUF 以下の追記が分割されない。
        #
        # **Windows はプロセス間の追記の原子性を保証しない。** ここは「短く保てば
        # 交錯しにくい」という緩和であって保証ではない。行が壊れうる前提で読むこと
        # （`rb history` は壊れた行を飛ばす）。
        handle = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
    except Exception:  # noqa: BLE001 - 監査の失敗で本処理を止めない
        return

    if stale:
        prune(root)


def prune(root: Path, *, max_age_days: float = AUDIT_RETENTION_DAYS) -> int:
    """古い監査ログを消す。消せた件数を返す。

    今日のファイルは書き込みが続いているので mtime が新しく、対象にならない。

    **失敗は全て無視する。** 掃除は付随的な仕事であり、ここで例外を出すのでは
    「掃除できないから宣言できない」という本末転倒になる。
    """
    cutoff = time.time() - max_age_days * 86400.0
    try:
        paths = list(audit_dir(root).glob("*.jsonl"))
    except OSError:
        return 0

    removed = 0
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _encode(record: dict[str, object]) -> bytes:
    """1 レコードを 1 行のバイト列にする。長すぎるものは**畳んで捨てる**。

    途中で切ると JSON として壊れ、その行は読む側から丸ごと消える
    （`rb history` は壊れた行を飛ばす）。切るくらいなら、
    「いつ・何のイベントが起きたか」だけを残した**妥当な JSON** に置き換える。
    長さの判定は文字数ではなく**エンコード後のバイト数**で行う。
    """
    line = json.dumps(record, ensure_ascii=False, default=str)
    data = (line + "\n").encode("utf-8", errors="replace")
    if len(data) <= MAX_LINE_BYTES:
        return data

    folded = {"at": record.get("at"), "event": record.get("event"), "truncated": True}
    return (json.dumps(folded, ensure_ascii=False, default=str) + "\n").encode(
        "utf-8", errors="replace"
    )
