"""PreToolUse フック: 資源を使いそうなコマンドの直前に、掲示板の現状を知らせる。

**このフックは何も止めない。** 以前は deny する強制層だったが、投入 3 分で誤爆した。
コマンド名のパターンでは「言及」と「実行」すら区別しきれず、まして「このコマンドが
資源を使うか」は原理的に分からない。**分かるのはセッションだけである。**

止めるのをやめて、代わりに**判断材料をその場に置く**。ツールが推測して禁じるのではなく、
セッションが知ったうえで決める。誤爆しても作業は止まらず、注意文が 1 つ増えるだけになる。

なぜ ``UserPromptSubmit`` だけでは足りないか
--------------------------------------------
毎プロンプトの注入は「掲示板を見ろ」という一般論であり、**薄い**。
ここは「いまから走らせようとしているこのコマンドは GPU0 を使いそうで、GPU0 は
folnet が使用中である」という**具体**を出せる。文書に書いてあるだけの規約は無視されるが、
その場に出た具体的な事実は無視しにくい。

設計上の約束
------------
- **必ず exit 0**。deny しない。止める判断はしない
- 判定表（``guard.json``）に一致したときだけ出す。表が無ければ黙る。**一律には出さない**
  （全 Bash に出すと、削ったはずの注入トークンを別の場所で払い直すことになる）
- 表が陳腐化しても害が無い。一致しなくなる＝注意が出なくなるだけである
- **stdlib のみ**。素の ``python`` で全セッションから呼ばれる
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ENCODING = "utf-8"

#: 何があっても通す。このフックは判断しない。
EXIT_ALLOW = 0

#: 判定表のファイル名（掲示板のルート直下）。
GUARD_FILE = "guard.json"

#: 1 コマンドあたりに評価する正規表現の上限。表が荒れても時間を使い切らない。
MAX_PATTERNS = 64

#: 引用符で囲まれた区間。中身は「実行されるコマンド」ではなくデータとみなす。
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)


def board_root() -> Path:
    """掲示板のルートを返す。本体の platform_info と同じ規則。"""
    override = os.environ.get("RESOURCE_BROKER_HOME")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "resource-broker"
    return Path.home() / ".resource-broker"


def executable_part(command: str) -> str:
    """コマンド文字列から、**実際に起動される部分**だけを取り出す。

    ヒアドキュメントの本文と引用符の中身を落とす。コマンドについて書くことと、
    コマンドを実行することは違う。区別しないと、ドキュメント編集も ``grep`` も
    コミットメッセージも「資源を使うコマンド」に見えてしまう（実際に起きた）。

    完全な shell の構文解析はしない。取りこぼす方向に倒れるが、**このフックは
    もう何も止めない**ので、取りこぼしの害は「注意が出ない」だけである。
    """
    head = command.split("<<", 1)[0]
    return _QUOTED.sub(" ", head)


def load_patterns(root: Path) -> list[dict[str, object]]:
    """判定表を読む。無い・読めない・壊れているは、いずれも「表が無い」とみなす。"""
    try:
        data = json.loads((root / GUARD_FILE).read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    patterns = data.get("patterns") if isinstance(data, dict) else None
    if not isinstance(patterns, list):
        return []
    return [p for p in patterns[:MAX_PATTERNS] if isinstance(p, dict) and p.get("pattern")]


def match(command: str, patterns: list[dict[str, object]]) -> dict[str, object] | None:
    """コマンドに一致する判定を返す。壊れた正規表現は飛ばす。"""
    target = executable_part(command)
    for entry in patterns:
        try:
            if re.search(str(entry["pattern"]), target):
                return entry
        except re.error:
            continue
    return None


def declaration_for(root: Path, resource: str | None) -> dict[str, object] | None:
    """その資源の宣言を返す。資源が指定されていなければ最初の 1 件。

    **判定はしない。** 幽霊かどうかは読む側が ``rb status`` で確かめる。
    """
    try:
        paths = sorted((root / "board").glob("*.json"))
    except OSError:
        return None

    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding=ENCODING))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or not entry.get("resource"):
            continue
        if resource is None:
            return entry
        declared = str(entry["resource"]).split("::", 1)[-1]
        if declared == resource:
            return entry
    return None


def build_notice(rule: dict[str, object], entry: dict[str, object] | None) -> str:
    """注意文を組み立てる。短く、具体的に。"""
    resource = rule.get("resource")
    target = str(resource) if resource else "有限資源"
    lines = [f"[rb] このコマンドは {target} を使う可能性があります。"]

    if entry is None:
        lines.append(f"  掲示板に {target} の宣言はありません（誰も使っていないとは限らない）。")
    else:
        holder = entry.get("holder")
        holder = holder if isinstance(holder, dict) else {}
        lines.append(
            f"  現状: {holder.get('session', '?')} / {holder.get('job') or '(ジョブ未記入)'}"
            f" (since {entry.get('since', '?')})"
        )
        eta = entry.get("eta")
        if isinstance(eta, dict) and eta.get("stated"):
            at = f"（{eta['at']} 頃）" if eta.get("at") else ""
            lines.append(f"  ETA: {eta['stated']}{at} ※申告であって約束ではない")

        usage = entry.get("usage")
        if isinstance(usage, dict) and (usage.get("peak") or usage.get("avg")):
            lines.append(
                f"  見積もり: 瞬時最大 {usage.get('peak') or '-'} / 平均 {usage.get('avg') or '-'}"
            )

        if entry.get("sharing"):
            lines.append(f"  相乗り: {entry['sharing']}（可否は当事者で決めること）")
        if entry.get("log"):
            lines.append(f"  log: {entry['log']}")

    lines.append("  使うなら自分で状態を調べ、rb run 経由で宣言すること。詳細は rb status。")
    return "\n".join(lines)


def emit(text: str) -> None:
    """注意文をセッションへ渡す。

    素のテキストを stdout へ書くだけでは**セッションに届かない**（transcript に出るだけで、
    Claude の文脈には入らない。実地で確認済み）。届けるには構造化した出力が要る。

    ``permissionDecision`` は使わない。``allow`` を返すと権限確認そのものを迂回してしまい、
    注意喚起のつもりで**全コマンドを自動承認する**ことになる。ここで欲しいのは
    「止めずに知らせる」ことだけである。

    UTF-8 のバイト列で書く（cp932 だと読む側で化ける）。
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": text,
        }
    }
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode(ENCODING, errors="replace")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError, OSError):
        try:
            sys.stdout.write(text + "\n")
        except Exception:  # noqa: BLE001 - fail-open
            pass


def notice_for(payload: dict[str, object]) -> str | None:
    """このツール呼び出しに出すべき注意文。無ければ None。"""
    if payload.get("tool_name") != "Bash":
        return None

    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = str(tool_input.get("command", ""))
    if not command:
        return None

    # rb 自身の呼び出しには出さない。既に掲示板を触っている
    if re.search(r"(^|[\\/\s])(rb|resource-broker)(\.exe)?\s", command):
        return None

    root = board_root()
    patterns = load_patterns(root)
    if not patterns:
        return None

    rule = match(command, patterns)
    if rule is None:
        return None

    resource = rule.get("resource")
    return build_notice(rule, declaration_for(root, str(resource) if resource else None))


def main() -> int:
    """フックの本体。何が起きても 0 を返す。"""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 - fail-open
        return EXIT_ALLOW

    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            notice = notice_for(payload)
            if notice:
                emit(notice)
    except Exception:  # noqa: BLE001 - fail-open。作業を止めない
        return EXIT_ALLOW
    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
