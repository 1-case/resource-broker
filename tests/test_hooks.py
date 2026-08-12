"""フックの守護テスト。

フックは**他の全セッションの起動経路に割り込む**。壊れたときにセッションの起動を
妨げてはならない（CLAUDE.md「Fail-Open」）。「注意する」だけでは守れないので、
壊れた出力・``rb`` の不在・異常終了のいずれでも exit 0 になることをテストで固定する。

フックは素の ``python`` で単体実行される想定なので、テストも**サブプロセスとして**
起動して検証する（import して呼ぶと、実運用と違う経路を検証してしまう）。

内容の検証には**実物の ``rb``** を使い、掲示板だけを一時ディレクトリへ差し替える。
偽コマンドで代用すると、フックと CLI の間の実際の受け渡しを検証できない。
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

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "sessionstart_notice.py"

#: ``rb`` に見せかける偽コマンドの中身。fail-open の検証にだけ使う。
FAKE_RB = """import sys
sys.stdout.write({payload!r})
sys.exit({code})
"""


def run_hook(
    *, home: Path | None = None, path: str | None = None, stdin: str = "{}"
) -> subprocess.CompletedProcess[str]:
    """フックをサブプロセスとして起動する。"""
    env = dict(os.environ)
    if home is not None:
        env["RESOURCE_BROKER_HOME"] = str(home)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


def declare(home: Path, resource: str, *, job: str, log: str | None = None) -> None:
    """一時掲示板に宣言を 1 件置く。"""
    board = Board(home)
    entry = build_entry(
        normalize(resource),
        job=job,
        log=log,
        session="folnet",
        observed={"note": "nvidia-smi: compute apps 1 件"},
    )
    assert board.try_claim(entry)


# --- 注入される内容 -------------------------------------------------------------


def test_busy_resource_is_reported(tmp_path: Path) -> None:
    """使用中の資源は、誰が・何を・どこで見られるかまで注入される。"""
    declare(tmp_path, "GPU0", job="E059 eval", log="C:\\logs\\job.log")

    result = run_hook(home=tmp_path)

    assert result.returncode == 0
    assert "GPU0" in result.stdout
    assert "folnet" in result.stdout
    assert "E059 eval" in result.stdout
    assert "job.log" in result.stdout  # 進捗の観測点を示す


def test_empty_board_still_explains_how_to_use(tmp_path: Path) -> None:
    """掲示板が空でも使い方は伝える。

    このフックの主目的は「掲示板の存在を知らせる」ことである。空のときに黙ると、
    宣言せずに資源を使うセッションが減らない。
    """
    result = run_hook(home=tmp_path)

    assert result.returncode == 0
    assert "rb run" in result.stdout


def test_notice_tells_the_session_to_investigate(tmp_path: Path) -> None:
    """「自分で調べる」ことを伝える。

    本ツールは資源を調べない。調べるのは受け取ったセッションの仕事であり、
    それが伝わらなければ `--observed` は形式的な記入欄になる。
    """
    result = run_hook(home=tmp_path)

    assert "自分で調べる" in result.stdout


# --- fail-open ------------------------------------------------------------------


def test_missing_rb_is_silent(tmp_path: Path) -> None:
    """``rb`` が入っていなくても、静かに 0 で通す。"""
    result = run_hook(home=tmp_path, path=str(tmp_path / "何も無い"))

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def make_fake_rb(directory: Path, payload: str, code: int = 0) -> str:
    """``rb`` という名前の偽コマンドを作り、それだけが載った PATH を返す。"""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "rb_impl.py"
    script.write_text(FAKE_RB.format(payload=payload, code=code), encoding="utf-8")

    if sys.platform == "win32":
        launcher = directory / "rb.bat"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "rb"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return str(directory)


@pytest.mark.parametrize("payload", ["", "{", "null", "[]", '{"resources": "文字列"}', "\x00"])
def test_broken_output_does_not_break_startup(tmp_path: Path, payload: str) -> None:
    """``rb`` の出力が壊れていてもセッションの起動を妨げない。"""
    result = run_hook(home=tmp_path, path=make_fake_rb(tmp_path / "bin", payload))

    assert result.returncode == 0


def test_failing_rb_is_silent(tmp_path: Path) -> None:
    """``rb`` が異常終了しても黙って通す。"""
    payload = json.dumps({"resources": [{"display": "GPU0", "free": False}]}, ensure_ascii=False)
    result = run_hook(home=tmp_path, path=make_fake_rb(tmp_path / "bin", payload, code=1))

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_corrupt_board_does_not_break_startup(tmp_path: Path) -> None:
    """掲示板が壊れていてもセッションの起動を妨げない。"""
    entries = tmp_path / "board"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "壊れた.json").write_text("{壊れている", encoding="utf-8")

    result = run_hook(home=tmp_path)

    assert result.returncode == 0


def test_empty_stdin_is_tolerated(tmp_path: Path) -> None:
    """フックへの入力が空でも落ちない。"""
    result = run_hook(home=tmp_path, stdin="")

    assert result.returncode == 0
