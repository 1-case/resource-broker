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


def bare_resource(resource_id: str) -> str:
    """資源 ID からホスト名のプレフィックスを外す。

    照合は**両側を同じ形にしてから**行う。片側だけ正規化すると、判定表に
    ``host::GPU0`` と書いた行が掲示板の ``host::GPU0`` に一致しなくなる
    （掲示板側だけ ``GPU0`` に落ちるため）。
    """
    return resource_id.split("::", 1)[-1]


def owned_by(declared_cwd: str, cwd: str) -> bool:
    """宣言者の場所が自分の場所と同じか、自分の祖先か。

    照合の規則を**本体（``Board.owns``）とそろえる**。ここだけ完全一致にすると、
    サブディレクトリで作業しているセッションが「自分は宣言していない」と判定され、
    宣言済みの相手に毎回「宣言しろ」と言うことになる。
    """
    if not declared_cwd or not cwd:
        return False
    parent = os.path.normcase(os.path.normpath(declared_cwd))
    child = os.path.normcase(os.path.normpath(cwd))
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def declarations_for(root: Path, resource: str | None) -> list[dict[str, object]]:
    """その資源の宣言（主宣言と相乗り）を返す。

    資源が特定されていない判定行では**無関係な宣言を返さない**。以前は掲示板の先頭 1 件を
    返しており、GPU を使うコマンドの直前に COM3 の宣言が「現状」として出ていた。
    その場に置いた具体的事実が嘘だと、以後の注意文全体が読まれなくなる。

    **判定はしない。** 幽霊かどうかは読む側が ``rb status`` で確かめる。
    """
    if resource is None:
        return []

    board = root / "board"
    found: list[dict[str, object]] = []
    for directory, is_join in ((board, False), (board / "joins", True)):
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                entry = json.loads(path.read_text(encoding=ENCODING))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict) or not entry.get("resource"):
                continue
            if bare_resource(str(entry["resource"])) == bare_resource(resource):
                entry["_join"] = is_join
                found.append(entry)
    return found


def declared_by_me(entries: list[dict[str, object]], cwd: str) -> bool:
    """その資源を自分が既に宣言しているか。

    宣言済みの相手に「宣言してから使え」と毎回言うのはノイズであり、ノイズは無視を招く。
    """
    for entry in entries:
        holder = entry.get("holder")
        holder = holder if isinstance(holder, dict) else {}
        if owned_by(str(holder.get("cwd", "")), cwd):
            return True
    return False


def build_notice(rule: dict[str, object], entries: list[dict[str, object]]) -> str:
    """注意文を組み立てる。短く、具体的に。"""
    resource = rule.get("resource")
    target = str(resource) if resource else "有限資源"
    lines = [f"[rb] このコマンドは {target} を使う可能性があります。"]
    if rule.get("note"):
        lines.append(f"  判定表の注記: {rule['note']}")

    if not entries:
        if resource:
            lines.append(
                f"  掲示板に {target} の宣言はありません（誰も使っていないとは限らない）。"
            )
        else:
            lines.append("  判定表にどの資源かが書かれていません。自分で特定すること。")

    for entry in entries:
        holder = entry.get("holder")
        holder = holder if isinstance(holder, dict) else {}
        kind = "相乗り" if entry.get("_join") else "現状"
        lines.append(
            f"  {kind}: {holder.get('session', '?')} / {holder.get('job') or '(ジョブ未記入)'}"
            f" (since {entry.get('since', '?')})"
        )
        eta = entry.get("eta")
        if isinstance(eta, dict) and eta.get("stated"):
            at = f"（{eta['at']} 頃）" if eta.get("at") else ""
            lines.append(f"    ETA: {eta['stated']}{at} ※申告であって約束ではない")

        usage = entry.get("usage")
        if isinstance(usage, dict) and (usage.get("peak") or usage.get("avg")):
            lines.append(
                f"    見積もり: 瞬時最大 {usage.get('peak') or '-'}"
                f" / 平均 {usage.get('avg') or '-'}"
            )

        if entry.get("sharing"):
            lines.append(f"    相乗り: {entry['sharing']}（可否は当事者で決めること）")
        if entry.get("log"):
            lines.append(f"    log: {entry['log']}")

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

    # rb 自身の呼び出しには出さない。既に掲示板を触っている。
    # 判定は「実際に起動される部分」に対して行う。文章として rb に言及しただけで
    # 黙ってしまうと、注意すべきコマンドを取りこぼす。
    runnable = executable_part(command)
    if re.search(r"(^|[\\/\s])(rb|resource-broker)(\.exe)?\s", runnable):
        return None

    root = board_root()
    patterns = load_patterns(root)
    if not patterns:
        return None

    rule = match(command, patterns)
    if rule is None:
        return None

    resource = rule.get("resource")
    entries = declarations_for(root, str(resource) if resource else None)

    # 既に自分が宣言している資源には出さない。宣言済みの相手に毎回「宣言しろ」と言うのは
    # ノイズであり、ノイズは無視を招く。
    cwd = str(payload.get("cwd") or os.getcwd())
    if declared_by_me(entries, cwd):
        return None

    return build_notice(rule, entries)


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
