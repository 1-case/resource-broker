"""SessionStart フック: 掲示板の現状をセッションのコンテキストへ注入する。

**このフックは何も止めない。知らせるだけである。** 掲示板が抱える最大の穴は
「他セッションが掲示板の存在を知らない」ことで、実際に本ツールの開発中、
別セッションが宣言せずに GPU を使っている状況を観測した。最初から知っていれば
deny に至らない、というのがこのフックの狙いである。

設計上の約束
------------
- **必ず exit 0**。本ツールが壊れてもセッションの起動を妨げてはならない（fail-open）
- **stdlib のみ**。他プロジェクトから素の ``python`` で呼ばれるため、
  ``uv run`` も本パッケージの import も前提にしない
- **判定を再実装しない**。幽霊判定は ``rb status --json`` に任せる。ここで自前の
  判定を書けば、本体と乖離した第 2 の真実ができる。``rb`` が無ければ黙って何も出さない

``rb status`` の実測応答時間は約 180ms である。SessionStart は 1 セッションに 1 回なので
許容できるが、**PreToolUse では使えない**（判定は 50ms 以内という要件がある）。
そちらは掲示板を直接読む必要がある。
"""

from __future__ import annotations

import json
import subprocess
import sys

#: ``rb status`` の待ち時間。超えたら黙って諦める。
TIMEOUT_S = 5.0

USAGE = """資源（GPU / COM ポート / ネットワークドライブ / ローカルポート等）を使う前に:
  1. その資源の状態を**自分で調べる**（調べ方はあなたが決める。本ツールは資源を知らない）
  2. rb run --res <資源ID> --job "<説明>" --observed "<何を見たか>"
            --found busy|free|unknown -- <コマンド>
     rb run は宣言・ログ・終了時の自動解放をまとめて行う。手動なら rb claim / rb release"""


def fetch_status() -> list[dict[str, object]] | None:
    """``rb status --json`` を呼んで資源の一覧を返す。取れなければ None。"""
    try:
        completed = subprocess.run(
            ["rb", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    resources = payload.get("resources") if isinstance(payload, dict) else None
    return resources if isinstance(resources, list) else None


def describe(resource: dict[str, object]) -> list[str]:
    """1 資源の状態を数行に整形する。"""
    holder = resource.get("holder") or {}
    holder = holder if isinstance(holder, dict) else {}
    display = resource.get("display") or resource.get("resource") or "?"
    session = holder.get("session", "?")
    job = holder.get("job") or "(ジョブ未記入)"

    lines = [f"  {display}  <- {session} / {job}"]
    if resource.get("since"):
        lines.append(f"      since {resource['since']}")
    if resource.get("log"):
        lines.append(f"      log   {resource['log']}  (進捗はここで読める)")

    observed = resource.get("observed") or {}
    if isinstance(observed, dict) and observed.get("note"):
        lines.append(f"      観測  {observed['note']}")
    return lines


def build_notice(resources: list[dict[str, object]]) -> str:
    """注入する本文を組み立てる。"""
    busy = [r for r in resources if isinstance(r, dict) and not r.get("free")]

    if not busy:
        return f"[resource-broker] 掲示板は空です（誰も資源を宣言していません）。\n{USAGE}"

    lines = ["[resource-broker] このマシンで使用中と宣言されている資源:"]
    for resource in busy:
        lines.extend(describe(resource))
    lines.append("")
    lines.append("上記は他セッションの宣言です。奪う前に必ず log を読み、状況を確認すること。")
    lines.append(USAGE)
    return "\n".join(lines)


def main() -> int:
    """フックの本体。何が起きても 0 を返す。"""
    try:
        sys.stdin.read()  # フックへの入力は使わないが、読み捨てて詰まらせない
    except Exception:  # noqa: BLE001 - fail-open
        pass

    try:
        resources = fetch_status()
        if resources is None:
            return 0  # rb が無い・壊れている。黙って通す
        sys.stdout.write(build_notice(resources) + "\n")
    except Exception:  # noqa: BLE001 - fail-open。起動を妨げない
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
