"""PreToolUse フック（強制層）の守護テスト。

**このフックだけが実際にコマンドを止める。** 全セッションの全 Bash 実行経路に割り込むため、
壊れ方が他と根本的に違う。ここで守るのは 2 つで、優先順位も決まっている。

1. **通すべきものを止めない**（誤 deny ゼロ）。壊れていたら必ず通す
2. 止めるべきものを止める

1 が 2 より重い。強制層のバグで全セッションが作業不能になるのが最悪の壊れ方である。
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

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "pretooluse_guard.py"

ALLOW = 0
DENY = 2


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


def declare(home: Path, resource: str, cwd: str, *, job: str = "宣言済みのジョブ") -> None:
    """指定の作業ディレクトリの持ち物として宣言を置く。"""
    board = Board(home)
    assert board.try_claim(build_entry(normalize(resource), job=job, cwd=cwd))


GPU_RULE = {"pattern": r"run_e\d+\.py", "resource": "GPU0", "note": "学習・評価スクリプト"}


# --- 既定では何も止めない -------------------------------------------------------


def test_nothing_is_blocked_without_a_guard_file(tmp_path: Path) -> None:
    """判定表が無ければ何も止めない。

    導入しただけで挙動が変わってはならない。強制層は明示的に有効化する。
    """
    result = run_hook(tmp_path, bash("python scripts/run_e059.py --stage eval"))

    assert result.returncode == ALLOW


def test_empty_pattern_list_blocks_nothing(tmp_path: Path) -> None:
    """判定表が空でも何も止めない。"""
    write_guard(tmp_path, [])

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == ALLOW


# --- 止めるべきものを止める -----------------------------------------------------


def test_unclaimed_resource_command_is_denied(tmp_path: Path) -> None:
    """一致するコマンドを宣言なしで実行しようとすると止まる。"""
    write_guard(tmp_path, [GPU_RULE])

    result = run_hook(tmp_path, bash("python scripts/run_e059.py --stage eval"))

    assert result.returncode == DENY
    reason = result.stderr.decode("utf-8")
    assert "rb run" in reason
    assert "GPU0" in reason
    assert "自分で調べる" in reason


def test_declared_resource_allows_the_command(tmp_path: Path) -> None:
    """自分が宣言していれば通す。"""
    write_guard(tmp_path, [GPU_RULE])
    declare(tmp_path, "GPU0", "C:\\works\\folnet")

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == ALLOW


def test_another_sessions_declaration_does_not_help(tmp_path: Path) -> None:
    """他セッションの宣言では通らない。

    「誰かが宣言しているから自分も使ってよい」は成り立たない。排他が壊れる。
    """
    write_guard(tmp_path, [GPU_RULE])
    declare(tmp_path, "GPU0", "C:\\works\\malm")

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == DENY


def test_declaring_a_different_resource_does_not_help(tmp_path: Path) -> None:
    """別の資源を宣言していても、指定された資源でなければ通らない。"""
    write_guard(tmp_path, [GPU_RULE])
    declare(tmp_path, "COM3", "C:\\works\\folnet")

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == DENY


def test_rule_without_resource_accepts_any_declaration(tmp_path: Path) -> None:
    """資源を特定しない判定は、何か宣言していれば通す。"""
    write_guard(tmp_path, [{"pattern": r"heavy_job\.py"}])
    declare(tmp_path, "\\\\nas\\share", "C:\\works\\folnet")

    assert run_hook(tmp_path, bash("python heavy_job.py")).returncode == ALLOW


def test_guard_is_not_resource_specific(tmp_path: Path) -> None:
    """資源の種別で扱いを変えない。GPU 以外でも同じように止まる。"""
    write_guard(tmp_path, [{"pattern": r"teach\.py", "resource": "COM3"}])

    denied = run_hook(tmp_path, bash("python teach.py")).returncode
    declare(tmp_path, "COM3", "C:\\works\\urcommander")
    allowed = run_hook(tmp_path, bash("python teach.py", cwd="C:\\works\\urcommander")).returncode

    assert (denied, allowed) == (DENY, ALLOW)


def test_rb_itself_is_never_blocked(tmp_path: Path) -> None:
    """``rb`` の呼び出しは止めない。

    止めると宣言する手段まで塞がれ、deny から抜け出せなくなる。
    """
    write_guard(tmp_path, [{"pattern": r"run_e\d+\.py", "resource": "GPU0"}])

    command = 'rb run --res GPU0 --job "x" --observed "y" -- python scripts/run_e059.py'
    assert run_hook(tmp_path, bash(command)).returncode == ALLOW


# --- 通すべきものを止めない（誤 deny ゼロ） -------------------------------------


@pytest.mark.parametrize(
    "command",
    ["git status", "ls -la", "uv run pytest", "echo run_e059", "cat notes.md"],
)
def test_unrelated_commands_pass(tmp_path: Path, command: str) -> None:
    """判定に当たらないコマンドは素通しする。"""
    write_guard(tmp_path, [GPU_RULE])

    assert run_hook(tmp_path, bash(command)).returncode == ALLOW


def test_mentioning_a_command_is_not_running_it(tmp_path: Path) -> None:
    """コマンド名に**言及しただけ**では止めない（回帰テスト）。

    投入初日に踏んだ誤 deny。STATUS.md を書き換えるコマンドのヒアドキュメントに
    スクリプト名が文章として含まれていたため、ドキュメント編集が止まった。
    区別できなければ grep もコミットメッセージも止まる。
    """
    write_guard(tmp_path, [GPU_RULE])
    command = "\n".join(
        [
            "python - <<'PY'",
            "text = '今日の事故コマンド（python -u scripts/run_e059.py --stage eval）'",
            "print(text)",
            "PY",
        ]
    )

    assert run_hook(tmp_path, bash(command)).returncode == ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'fix: run_e059.py の宣言漏れを直す'",
        'grep -rn "run_e059.py" docs/',
        "echo 'run_e059.py を実行する前に宣言すること'",
        'rb claim GPU0 --job "run_e059.py の準備" --observed "調べた"',
    ],
)
def test_quoted_text_does_not_trigger(tmp_path: Path, command: str) -> None:
    """引用符の中の文字列はデータであって、起動されるコマンドではない。"""
    write_guard(tmp_path, [GPU_RULE])

    assert run_hook(tmp_path, bash(command)).returncode == ALLOW


def test_real_invocation_is_still_denied(tmp_path: Path) -> None:
    """言及を通すようにしても、素の実行はきちんと止める。"""
    write_guard(tmp_path, [GPU_RULE])

    for command in (
        "python -u scripts/run_e059.py --stage eval",
        "cd folnet && uv run python scripts/run_e060.py",
        "python scripts/run_e059.py > out.log 2>&1",
    ):
        assert run_hook(tmp_path, bash(command)).returncode == DENY, command


def test_other_tools_are_not_touched(tmp_path: Path) -> None:
    """Bash 以外のツールには関与しない。"""
    write_guard(tmp_path, [GPU_RULE])
    payload = {"tool_name": "Read", "tool_input": {"file_path": "run_e059.py"}}

    assert run_hook(tmp_path, payload).returncode == ALLOW


# --- fail-open ------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"null", b"[]", b'{"tool_name": 42}', b"\x00\x01\x02"],
)
def test_broken_input_passes(tmp_path: Path, payload: bytes) -> None:
    """入力が壊れていても通す。"""
    write_guard(tmp_path, [GPU_RULE])

    assert run_hook(tmp_path, payload).returncode == ALLOW


@pytest.mark.parametrize("content", ["", "{", "null", '{"patterns": "文字列"}', "\x00"])
def test_broken_guard_file_passes(tmp_path: Path, content: str) -> None:
    """判定表が壊れていたら止めない。

    壊れた強制層で全セッションを止めるのが最悪の結果である。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "guard.json").write_text(content, encoding="utf-8")

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == ALLOW


def test_broken_regex_is_skipped(tmp_path: Path) -> None:
    """壊れた正規表現があっても、他の判定は生きる。"""
    write_guard(tmp_path, [{"pattern": "([壊れている"}, GPU_RULE])

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == DENY


def test_corrupt_board_does_not_block(tmp_path: Path) -> None:
    """掲示板が壊れていても、宣言があるものは読めれば通す。"""
    write_guard(tmp_path, [GPU_RULE])
    entries = tmp_path / "board"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "壊れた.json").write_text("{壊れている", encoding="utf-8")
    declare(tmp_path, "GPU0", "C:\\works\\folnet")

    assert run_hook(tmp_path, bash("python scripts/run_e059.py")).returncode == ALLOW


def test_deny_reason_is_utf8(tmp_path: Path) -> None:
    """理由文は UTF-8（cp932 で書くと読む側で化ける）。"""
    write_guard(tmp_path, [GPU_RULE])

    result = run_hook(tmp_path, bash("python scripts/run_e059.py"))

    assert "資源" in result.stderr.decode("utf-8")
