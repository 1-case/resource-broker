"""PreToolUse フック: 未宣言のまま資源を使おうとするコマンドを止める。

本ツールで唯一の**強制**である。ここまでのフックは知らせるだけで、無視して実行できた。

安全側の設計
------------
このフックは**全セッションの全 Bash 実行経路に割り込む**。壊れ方が他と根本的に違うため、
次を守る。

- **既定では何も止めない。** 判定表（``guard.json``）が無ければ即 exit 0。
  導入しただけでは挙動が変わらない
- **一致しなければ即 exit 0。** 素通しが既定であり、deny-by-default にはしない
- **内部エラーでは必ず exit 0。** 掲示板の破損・判定表の破損・想定外の例外のいずれでも通す。
  本ツールの不具合で全セッションの作業を止めてはならない
- **待機しない。** ここでブロックするとセッションが固まり、Esc でも抜けられない

資源を知らないままどう止めるか
------------------------------
本ツールは資源の種別を知らないし、「どのコマンドが資源を使うか」も判別できない。
そこで判別だけを**データ**（``guard.json``）に出し、コードは「一致したか」しか見ない。

プローブの調べ方を登録させる案は却下した（陳腐化すると「空きに見える」＝危険側に倒れる）。
判定表は**壊れ方の向きが逆**である。陳腐化しても一致しなくなるだけで、
**素通し＝ fail-open 側**に倒れる。だからこちらは持ってよい。

判定表の場所は掲示板と同じルートの ``guard.json``。マシン全体で 1 箇所であり、
プロジェクトごとの設定は要らない。

deny の伝え方
-------------
``PreToolUse`` は **exit 2 で拒否**し、stderr の内容がセッションへ返る。
そこに「何をすればよいか」を書く。判別できないのはこちら側の限界なので、
**何をどう調べるかは受け取ったセッションに委ねる**。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ENCODING = "utf-8"

EXIT_ALLOW = 0
EXIT_DENY = 2

#: 判定表のファイル名（掲示板のルート直下）。
GUARD_FILE = "guard.json"

#: 1 コマンドあたりに評価する正規表現の上限。表が荒れても時間を使い切らない。
MAX_PATTERNS = 64


def board_root() -> Path:
    """掲示板のルートを返す。本体の platform_info と同じ規則。"""
    override = os.environ.get("RESOURCE_BROKER_HOME")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "resource-broker"
    return Path.home() / ".resource-broker"


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


#: 引用符で囲まれた区間。中身は「実行されるコマンド」ではなくデータとみなす。
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)


def executable_part(command: str) -> str:
    """コマンド文字列から、**実際に起動される部分**だけを取り出す。

    投入初日に自分自身を deny した。STATUS.md を書き換えるコマンドのヒアドキュメントに
    スクリプト名が**文章として**含まれていたためである。**コマンドについて書くことと、
    コマンドを実行することは違う。** 区別できなければ、ドキュメントの編集も grep も
    コミットメッセージも止まる。

    完全な shell の構文解析はしない（できないし、するべきでもない）。データが載りやすい
    2 か所だけを落とす。

    - ヒアドキュメント（``<<`` 以降）。本文はコマンドではない
    - 引用符で囲まれた区間。``--job "E059 の再実行"`` のような説明文が入る

    取りこぼす方向に倒れる（``bash -c "python train.py"`` は一致しなくなる）。
    **誤って止めるより、取りこぼすほうがよい**という優先順位に従う。
    """
    head = command.split("<<", 1)[0]
    return _QUOTED.sub(" ", head)


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


def same_place(left: str, right: str) -> bool:
    """2 つのパスが同じ場所を指すか。Windows は大小と区切りを無視する。"""

    def normalize(path: str) -> str:
        return os.path.normcase(os.path.normpath(path.strip()))

    return bool(left) and bool(right) and normalize(left) == normalize(right)


def declarations_by(root: Path, cwd: str) -> list[dict[str, object]]:
    """この作業ディレクトリが持っている宣言を返す。"""
    try:
        paths = sorted((root / "board").glob("*.json"))
    except OSError:
        return []

    mine: list[dict[str, object]] = []
    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding=ENCODING))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        holder = entry.get("holder")
        holder = holder if isinstance(holder, dict) else {}
        if same_place(str(holder.get("cwd", "")), cwd):
            mine.append(entry)
    return mine


def covers(entry: dict[str, object], resource: str) -> bool:
    """その宣言が指定の資源を含むか。ホスト名プレフィックスは無視して比べる。"""
    declared = str(entry.get("resource", ""))
    tail = declared.split("::", 1)[-1]
    return tail == resource or declared == resource


def build_reason(rule: dict[str, object], resource: str | None) -> str:
    """deny の理由文を組み立てる。何をすればよいかまで書く。"""
    target = resource or "<資源ID>"
    lines = [
        "resource-broker: 有限資源を使う可能性のあるコマンドですが、宣言がありません。",
    ]
    if rule.get("note"):
        lines.append(f"  判定表の注記: {rule['note']}")
    lines += [
        "",
        "次の手順で実行してください。",
        f"  1. {target} の状態を自分で調べる（調べ方はあなたが決める。本ツールは資源を知らない）",
        f'  2. rb run --res {target} --job "<説明>" --observed "<何を見たか>"'
        " --found busy|free|unknown -- <コマンド>",
        "",
        "他セッションの状況は rb status で確認できます。",
        "この判定が誤りなら、掲示板ルートの guard.json から該当パターンを外してください。",
    ]
    return "\n".join(lines)


def emit_stderr(text: str) -> None:
    """UTF-8 のバイト列で stderr へ書く（cp932 だと読む側で化ける）。"""
    data = (text + "\n").encode(ENCODING, errors="replace")
    try:
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    except (AttributeError, ValueError, OSError):
        try:
            sys.stderr.write(text + "\n")
        except Exception:  # noqa: BLE001 - fail-open
            pass


def decide(payload: dict[str, object]) -> tuple[int, str]:
    """通すか止めるかを決める。``(終了コード, 理由)`` を返す。"""
    if payload.get("tool_name") != "Bash":
        return EXIT_ALLOW, ""

    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = str(tool_input.get("command", ""))
    if not command:
        return EXIT_ALLOW, ""

    root = board_root()
    patterns = load_patterns(root)
    if not patterns:
        return EXIT_ALLOW, ""  # 判定表が無い＝何も止めない（既定）

    rule = match(command, patterns)
    if rule is None:
        return EXIT_ALLOW, ""

    # rb 自身の呼び出しは止めない。止めると宣言する手段まで塞がれる
    if re.search(r"(^|[\\/\s])(rb|resource-broker)(\.exe)?\s", command):
        return EXIT_ALLOW, ""

    cwd = str(payload.get("cwd") or os.getcwd())
    mine = declarations_by(root, cwd)
    resource = rule.get("resource")
    resource = str(resource) if resource else None

    if resource is None:
        satisfied = bool(mine)
    else:
        satisfied = any(covers(entry, resource) for entry in mine)

    if satisfied:
        return EXIT_ALLOW, ""
    return EXIT_DENY, build_reason(rule, resource)


def main() -> int:
    """フックの本体。内部エラーでは必ず 0 を返して通す。"""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 - fail-open
        return EXIT_ALLOW

    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return EXIT_ALLOW
        code, reason = decide(payload)
    except Exception:  # noqa: BLE001 - fail-open。作業を止めない
        return EXIT_ALLOW

    if code == EXIT_DENY:
        emit_stderr(reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
