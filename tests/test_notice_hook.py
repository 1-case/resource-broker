"""PreToolUse フック（注意喚起）の守護テスト。

このフックは**何も止めない**。以前は deny する強制層だったが、投入 3 分で誤爆したため
「止める」のをやめ、「判断材料をその場に置く」だけにした。

したがってここで最も重く守るのは **deny しないこと**である。うっかり止める実装に戻ると、
全セッションの Bash が本ツールの都合で止まりうる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "pretooluse_notice.py"

ALLOW = 0

RULE = {"pattern": r"run_e\d+\.py", "resource": "GPU0", "note": "実験スクリプト"}


def run_hook(home: Path, payload: object) -> subprocess.CompletedProcess[bytes]:
    """フックを起動する。stdin には PreToolUse の入力を渡す。"""
    env = dict(os.environ)
    env["RESOURCE_BROKER_HOME"] = str(home)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return subprocess.run(
        [sys.executable, str(HOOK)], input=raw, capture_output=True, env=env, timeout=60
    )


def bash(command: str, cwd: str = "C:\\works\\folnet") -> dict[str, object]:
    """Bash ツールの PreToolUse 入力を組み立てる。"""
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def write_guard(home: Path, patterns: list[dict[str, object]]) -> None:
    """判定表を置く。"""
    home.mkdir(parents=True, exist_ok=True)
    (home / "guard.json").write_text(
        json.dumps({"schema": 1, "patterns": patterns}, ensure_ascii=False), encoding="utf-8"
    )


def notice_of(result: subprocess.CompletedProcess[bytes]) -> str:
    """出力から注意文を取り出す。出ていなければ空文字。"""
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    payload = json.loads(text)
    return payload["hookSpecificOutput"]["additionalContext"]


# --- 何も止めないこと（最優先） -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "python scripts/run_e059.py --stage eval",
        "rm -rf /",
        "python -u scripts/run_e060.py",
    ],
)
def test_never_denies(tmp_path: Path, command: str) -> None:
    """どんなコマンドでも止めない。

    一致しようがしまいが exit 0 である。ここが崩れると、全セッションの Bash が
    本ツールの都合で止まりうる。
    """
    write_guard(tmp_path, [RULE])

    assert run_hook(tmp_path, bash(command)).returncode == ALLOW


def test_does_not_emit_permission_decision(tmp_path: Path) -> None:
    """``permissionDecision`` を返さない。

    ``allow`` を返すと権限確認そのものを迂回する。注意喚起のつもりで
    **全コマンドを自動承認する**ことになり、注意より遥かに大きな害になる。
    """
    write_guard(tmp_path, [RULE])

    text = run_hook(tmp_path, bash("python scripts/run_e059.py")).stdout.decode("utf-8")

    assert "permissionDecision" not in text


# --- 注意文の中身 ---------------------------------------------------------------


def test_notice_reaches_the_session_as_additional_context(tmp_path: Path) -> None:
    """素のテキストではセッションに届かないため、構造化して返す。

    実地で確認済み: exit 0 で stdout に平文を書いても transcript に出るだけで、
    Claude の文脈には入らない。
    """
    write_guard(tmp_path, [RULE])

    result = run_hook(tmp_path, bash("python scripts/run_e059.py"))
    payload = json.loads(result.stdout.decode("utf-8"))

    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "GPU0" in payload["hookSpecificOutput"]["additionalContext"]


def test_notice_shows_the_current_holder(tmp_path: Path) -> None:
    """誰が使っているかを具体的に出す。

    毎プロンプトの注入は「掲示板を見ろ」という一般論で薄い。ここは
    「いま走らせようとしているこれは GPU0 を使いそうで、GPU0 は folnet が使用中」
    という具体を出せる。
    """
    write_guard(tmp_path, [RULE])
    board = Board(tmp_path)
    entry = build_entry(
        normalize("GPU0"),
        job="E059 eval",
        session="folnet",
        eta="40m",
        peak="VRAM 1.4GB / 瞬時最大 20%",
        sharing="可（要連絡）",
    )
    assert board.try_claim(entry)

    notice = notice_of(run_hook(tmp_path, bash("python scripts/run_e059.py")))

    assert "folnet" in notice
    assert "E059 eval" in notice
    assert "40m" in notice
    assert "瞬時最大 20%" in notice
    assert "可（要連絡）" in notice


def test_absence_of_declaration_is_not_reported_as_free(tmp_path: Path) -> None:
    """宣言が無いことを「空き」と言い切らない。

    掲示板に載っていないことは「誰も使っていない」ではない。実際、宣言せずに
    GPU を掴んでいたセッションを観測している。
    """
    write_guard(tmp_path, [RULE])

    notice = notice_of(run_hook(tmp_path, bash("python scripts/run_e059.py")))

    assert "誰も使っていないとは限らない" in notice


# --- 黙るべきとき ---------------------------------------------------------------


def test_silent_without_a_guard_file(tmp_path: Path) -> None:
    """判定表が無ければ黙る。"""
    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).stdout.strip() == b""


def test_silent_when_nothing_matches(tmp_path: Path) -> None:
    """一致しなければ黙る。全 Bash に出すと注入トークンを払い直すことになる。"""
    write_guard(tmp_path, [RULE])

    assert run_hook(tmp_path, bash("git status")).stdout.strip() == b""


def test_silent_for_rb_itself(tmp_path: Path) -> None:
    """``rb`` の呼び出しには出さない。既に掲示板を触っている。"""
    write_guard(tmp_path, [RULE])
    command = 'rb run --res GPU0 --job "x" --observed "y" -- python scripts/run_e059.py'

    assert run_hook(tmp_path, bash(command)).stdout.strip() == b""


def test_mentioning_is_not_running(tmp_path: Path) -> None:
    """コマンド名に言及しただけでは出さない（誤爆の回帰テスト）。"""
    write_guard(tmp_path, [RULE])
    command = "python - <<'PY'\nprint('scripts/run_e059.py を実行する前に宣言する')\nPY"

    assert run_hook(tmp_path, bash(command)).stdout.strip() == b""


def test_other_tools_are_not_touched(tmp_path: Path) -> None:
    """Bash 以外のツールには関与しない。"""
    write_guard(tmp_path, [RULE])
    payload = {"tool_name": "Read", "tool_input": {"file_path": "scripts/run_e059.py"}}

    assert run_hook(tmp_path, payload).stdout.strip() == b""


# --- fail-open ------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", [b"", b"{", b"null", b"[]", b'{"tool_name": 42}', b"\x00\x01\x02"]
)
def test_broken_input_is_tolerated(tmp_path: Path, payload: bytes) -> None:
    """入力が壊れていても通す。"""
    write_guard(tmp_path, [RULE])

    assert run_hook(tmp_path, payload).returncode == ALLOW


@pytest.mark.parametrize("content", ["", "{", "null", '{"patterns": "文字列"}', "\x00"])
def test_broken_guard_file_is_tolerated(tmp_path: Path, content: str) -> None:
    """判定表が壊れていても通す。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "guard.json").write_text(content, encoding="utf-8")

    result = run_hook(tmp_path, bash("python scripts/run_e059.py"))

    assert result.returncode == ALLOW
    assert result.stdout.strip() == b""


def test_broken_regex_is_skipped(tmp_path: Path) -> None:
    """壊れた正規表現があっても、他の判定は生きる。"""
    write_guard(tmp_path, [{"pattern": "([壊れている"}, RULE])

    assert "GPU0" in notice_of(run_hook(tmp_path, bash("python scripts/run_e059.py")))


def test_corrupt_board_is_tolerated(tmp_path: Path) -> None:
    """掲示板が壊れていても注意は出せる。"""
    write_guard(tmp_path, [RULE])
    entries = tmp_path / "board"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "壊れた.json").write_text("{壊れている", encoding="utf-8")

    result = run_hook(tmp_path, bash("python scripts/run_e059.py"))

    assert result.returncode == ALLOW
    assert "GPU0" in notice_of(result)


def test_notice_is_utf8(tmp_path: Path) -> None:
    """注意文は UTF-8（cp932 で書くと読む側で化ける）。"""
    write_guard(tmp_path, [RULE])

    assert "掲示板" in notice_of(run_hook(tmp_path, bash("python scripts/run_e059.py")))
