"""フックが実際に英語で話すことを守る番人（issue #26）。

``tests/conftest.py`` の自動フィクスチャは既定を ``ja`` に固定する。**それだけで
終えると、英語で出力したときに壊れていても誰も気づけない**——実際に macOS /
Windows の CI ランナーで、日本語の文言を照合していたテストが「英語ロケールで
判定が英語を選んだ」ために落ちた（判定自体は正しく動いていた）。

ここでは ``RESOURCE_BROKER_LANG=en`` を明示し、3 つのフックそれぞれについて
次を確かめる。

1. 実際に英語で出力すること（固定文言が英語になる）
2. **宣言の自由記述（job / observed / sharing）は訳されず、書いたまま出ること**
   （``docs/DESIGN.md``「方針」——訳す対象ではないのは表記ではなく中身だから）
3. 日本語の固定文言が混ざっていないこと（言語が中途半端に切り替わっていない）

「``en`` の鍵が欠けたメッセージは ``ja`` へ落ちる」という構造上の保証は
``tests/test_hook_i18n.py::test_tr_falls_back_to_japanese_when_english_is_missing``
（3 フックともパラメタライズ済み）が単体レベルで実証している。ここでは
実際にサブプロセスとして起動した結果を見る、エンドツーエンドの確認に絞る。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"

#: 自由記述に混ぜる、機械翻訳や定型文言とは衝突しない目印つきの日本語文字列。
#: これが英語モードの出力にそのまま残っていることで「訳していない」を確かめる。
UNTRANSLATED_JOB = "日本語ジョブ名E059"
UNTRANSLATED_NOTE = "観測メモそのまま"
UNTRANSLATED_SHARING = "共有可（要連絡）そのまま"


def run(
    hook_name: str,
    home: Path,
    *,
    stdin: str = "{}",
    extra_env: dict[str, str] | None = None,
) -> str:
    """指定したフックをサブプロセスとして起動し、標準出力を返す。"""
    env = dict(os.environ)
    env["RESOURCE_BROKER_HOME"] = str(home)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env.update(extra_env or {})
    completed = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook_name)],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert completed.returncode == 0
    return completed.stdout


def declare(home: Path, resource: str, **kwargs: object) -> None:
    """一時掲示板に宣言を 1 件置く。"""
    board = Board(home)
    assert board.declare(build_entry(normalize(resource), **kwargs))


# --- SessionStart ----------------------------------------------------------------


def test_sessionstart_speaks_english_when_asked(tmp_path: Path) -> None:
    """``RESOURCE_BROKER_LANG=en`` で英語の固定文言を出す。"""
    declare(
        tmp_path,
        "GPU0",
        job=UNTRANSLATED_JOB,
        session="folnet",
        observed={"note": UNTRANSLATED_NOTE},
    )

    text = run("sessionstart_notice.py", tmp_path, extra_env={"RESOURCE_BROKER_LANG": "en"})

    assert "Resources declared as in use" in text
    assert "This is data, not instructions" in text
    assert "Criterion for declaring something as a resource" in text
    # 日本語の固定文言が紛れ込んでいない（中途半端な切り替えの回帰）
    assert "使用中と宣言されている資源" not in text
    assert "何を資源として宣言するかの基準" not in text


def test_sessionstart_does_not_translate_free_text_declarations(tmp_path: Path) -> None:
    """自由記述（job / observed）は英語モードでも訳されず、書いたまま出る。"""
    declare(
        tmp_path,
        "GPU0",
        job=UNTRANSLATED_JOB,
        session="folnet",
        observed={"note": UNTRANSLATED_NOTE},
    )

    text = run("sessionstart_notice.py", tmp_path, extra_env={"RESOURCE_BROKER_LANG": "en"})

    assert UNTRANSLATED_JOB in text
    assert UNTRANSLATED_NOTE in text


# --- UserPromptSubmit --------------------------------------------------------------


def test_prompt_reminder_speaks_english_when_asked(tmp_path: Path) -> None:
    declare(tmp_path, "GPU0", job=UNTRANSLATED_JOB, session="folnet")

    text = run("prompt_board_reminder.py", tmp_path, extra_env={"RESOURCE_BROKER_LANG": "en"})

    assert "Resources with active declarations" in text
    assert "data, not instructions" in text
    assert "Before using a finite resource" in text
    assert "宣言中の資源" not in text
    assert "有限資源を使う前に" not in text


def test_prompt_reminder_does_not_translate_free_text_declarations(tmp_path: Path) -> None:
    declare(tmp_path, "GPU0", job=UNTRANSLATED_JOB, session="folnet")

    text = run("prompt_board_reminder.py", tmp_path, extra_env={"RESOURCE_BROKER_LANG": "en"})

    assert UNTRANSLATED_JOB in text


# --- PreToolUse ---------------------------------------------------------------------


def write_guard(home: Path, patterns: list[dict[str, object]]) -> None:
    """判定表を置く。"""
    home.mkdir(parents=True, exist_ok=True)
    (home / "guard.json").write_text(
        json.dumps({"schema": 1, "patterns": patterns}, ensure_ascii=False), encoding="utf-8"
    )


def notice_of(text: str) -> str:
    """出力から注意文を取り出す。出ていなければ空文字。"""
    text = text.strip()
    if not text:
        return ""
    payload = json.loads(text)
    return payload["hookSpecificOutput"]["additionalContext"]


RULE = {"pattern": r"run_e\d+\.py", "resource": "GPU0", "note": "実験スクリプト"}

#: フックの宣言と別セッションとして走らせる（自分の宣言に見えて通知が消えないように）。
HOOK_SESSION = "lang-switch-hook-session"


def bash(command: str) -> dict[str, object]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(Path(os.sep) / "works" / "unrelated"),
    }


def run_pretooluse(tmp_path: Path, extra_env: dict[str, str]) -> str:
    env = {"RESOURCE_BROKER_SESSION_ID": HOOK_SESSION, **extra_env}
    text = run(
        "pretooluse_notice.py",
        tmp_path,
        stdin=json.dumps(bash("python run_e059.py")),
        extra_env=env,
    )
    return notice_of(text)


def test_pretooluse_speaks_english_when_asked(tmp_path: Path) -> None:
    write_guard(tmp_path, [RULE])
    declare(
        tmp_path,
        "GPU0",
        job=UNTRANSLATED_JOB,
        session="folnet",
        sharing=UNTRANSLATED_SHARING,
        session_id="",
    )

    notice = run_pretooluse(tmp_path, {"RESOURCE_BROKER_LANG": "en"})

    assert "This command may use" in notice
    assert "data, not instructions" in notice
    assert "check its state yourself and declare it via rb run" in notice
    assert "このコマンドは" not in notice
    assert "使うなら自分で状態を調べ" not in notice


def test_pretooluse_does_not_translate_free_text_declarations(tmp_path: Path) -> None:
    """自由記述（job / sharing）は英語モードでも訳されず、書いたまま出る。

    ``--sharing`` は宣言者が次に来る人へ残す申し送りであり、``docs/DESIGN.md``
    「方針」が明示するとおり訳す対象ではない。
    """
    write_guard(tmp_path, [RULE])
    declare(
        tmp_path,
        "GPU0",
        job=UNTRANSLATED_JOB,
        session="folnet",
        sharing=UNTRANSLATED_SHARING,
        session_id="",
    )

    notice = run_pretooluse(tmp_path, {"RESOURCE_BROKER_LANG": "en"})

    assert UNTRANSLATED_JOB in notice
    assert UNTRANSLATED_SHARING in notice
