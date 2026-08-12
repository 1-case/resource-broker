"""UserPromptSubmit フック: 毎プロンプトで掲示板の現状と規約を注入する。

なぜ ``SessionStart`` だけでは足りないか
----------------------------------------
起動時の 1 回では、**資源を使い始める瞬間に何も言えない**。実際に事故が起きた。
あるセッションは起動の 25 分後に GPU を掴んだが、その時点で通知はすでに過去のもので、
掲示板は「空」のまま更新されなかった。

**このフックは資源も、コマンドの種類も知らない。** 判別を一切しないので、
どの資源にも、どのプロジェクトにも、同じように効く。各プロジェクトの CLAUDE.md を
書き換える必要がない（プロジェクト数が増えるほど、そちらは維持できなくなる）。

設計上の約束
------------
- **必ず exit 0**。壊れてもユーザーの入力を妨げてはならない（fail-open）
- **stdlib のみ**。素の ``python`` で全セッションから呼ばれる
- **判定しない**。幽霊判定は行わず、掲示板に載っている宣言をそのまま並べる。
  毎プロンプト走るため ``rb`` の起動（実測 180ms）を避ける必要があり、
  かつ判定を再実装すれば本体と乖離した第 2 の真実ができる。**どちらも避けて「判定しない」**
- **短く保つ**。毎回入るものなので、**使い方は書かない**。資源の例示もコマンドの書式も
  ``SessionStart`` が 1 回だけ出す。ここが持つのは「いま誰が何を宣言しているか」と、
  ``rb run`` を通せという一言だけである。全ターンの文脈に積み上がるため、
  1 文字の重みが他のフックと違う
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ENCODING = "utf-8"

#: 一度に並べる宣言の上限。掲示板が荒れていても注入を膨らませない。
MAX_ENTRIES = 8

#: 毎ターン積み上がるため、宣言が無いときの注入はこの文字数を超えないようにする。
#: 使い方の説明は SessionStart 側に置き、ここには持ち込まない。
IDLE_BUDGET_CHARS = 60

RULE = "有限資源を使う前に自分で状態を調べ、rb run 経由で実行すること。"


def board_root() -> Path:
    """掲示板のルートを返す。本体の platform_info と同じ規則。

    import はしない。他プロジェクトから素の ``python`` で呼ばれるため、
    本パッケージが入っていない前提で動く必要がある。
    """
    override = os.environ.get("RESOURCE_BROKER_HOME")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "resource-broker"
    return Path.home() / ".resource-broker"


def read_entries(root: Path) -> list[dict[str, object]]:
    """掲示板の宣言を読む。読めないものは黙って飛ばす。

    **判定はしない。** 幽霊かどうかは読む側が `rb status` で確かめる。
    """
    try:
        paths = sorted((root / "board").glob("*.json"))
    except OSError:
        return []

    entries: list[dict[str, object]] = []
    for path in paths[:MAX_ENTRIES]:
        try:
            data = json.loads(path.read_text(encoding=ENCODING))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("resource"):
            entries.append(data)
    return entries


def build_notice(entries: list[dict[str, object]]) -> str:
    """注入する本文を組み立てる。

    宣言が無ければ 1 行。あれば「誰が何を」だけを並べる。
    ログのパスや観測メモは載せない（長いうえ、判断が要る場面でしか使わない）。
    必要になった側が ``rb status`` を叩けば全部読める。
    """
    if not entries:
        return f"[rb] 宣言なし。{RULE}"

    lines = ["[rb] 宣言中の資源:"]
    for entry in entries:
        holder = entry.get("holder")
        holder = holder if isinstance(holder, dict) else {}
        display = entry.get("display") or entry.get("resource")
        session = holder.get("session", "?")
        job = holder.get("job") or "(ジョブ未記入)"
        lines.append(f"  {display} <- {session} / {job} (since {entry.get('since', '?')})")
    lines.append(f"詳細は rb status。{RULE}")
    return "\n".join(lines)


def emit(text: str) -> None:
    """UTF-8 のバイト列として書き出す。

    Windows では ``sys.stdout.encoding`` が cp932 になる。テキスト層を通すと
    読む側で文字化けする（``SessionStart`` フックで実際に起きた）。
    """
    data = (text + "\n").encode(ENCODING, errors="replace")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError, OSError):
        try:
            sys.stdout.write(text + "\n")
        except Exception:  # noqa: BLE001 - fail-open
            pass


def main() -> int:
    """フックの本体。何が起きても 0 を返す。"""
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 - fail-open
        pass

    try:
        emit(build_notice(read_entries(board_root())))
    except Exception:  # noqa: BLE001 - fail-open。入力を妨げない
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
