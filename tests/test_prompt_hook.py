"""UserPromptSubmit フックの守護テスト。

このフックは**全セッションの全プロンプト**に割り込む。壊れたときにユーザーの入力を
妨げてはならないし、遅くてもいけない。「注意する」だけでは守れないので固定する。

`SessionStart` フックと違い、``rb`` を起動しない。毎プロンプト走るため 180ms を
払えないのと、判定を再実装しないためである（判定そのものをしない）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "prompt_board_reminder.py"

#: 毎プロンプト走るため、上限を決めて守る。``rb`` の起動（実測 180ms）は避ける。
BUDGET_S = 1.0


def run_hook(home: Path, *, path: str | None = None) -> bytes:
    """フックを起動し、生バイトを返す。"""
    env = dict(os.environ)
    env["RESOURCE_BROKER_HOME"] = str(home)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    if path is not None:
        env["PATH"] = path
    completed = subprocess.run(
        [sys.executable, str(HOOK)], input=b"{}", capture_output=True, env=env, timeout=60
    )
    assert completed.returncode == 0
    return completed.stdout


def declare(home: Path, resource: str, *, job: str, session: str = "folnet") -> None:
    """一時掲示板に宣言を 1 件置く。"""
    board = Board(home)
    assert board.try_claim(build_entry(normalize(resource), job=job, session=session))


def test_rule_is_injected_even_when_board_is_empty(tmp_path: Path) -> None:
    """宣言が無くても規約は毎回入る。

    事故は「掲示板が空のときに、宣言せず使い始めた」形で起きた。
    空のときに黙ると、**まさにその相手に何も言えない**。
    """
    text = run_hook(tmp_path).decode("utf-8")

    assert "rb run" in text
    assert "自分でその資源の状態を調べ" in text


def test_declarations_are_listed(tmp_path: Path) -> None:
    """宣言があれば、誰が何をしているかを並べる。"""
    declare(tmp_path, "GPU0", job="E059 eval")

    text = run_hook(tmp_path).decode("utf-8")

    assert "GPU0" in text
    assert "folnet" in text
    assert "E059 eval" in text


def test_notice_is_not_resource_specific(tmp_path: Path) -> None:
    """規約文が特定の資源に寄っていない。

    GPU だけの仕組みにしないという原則を、注入文のレベルでも守る。
    """
    text = run_hook(tmp_path).decode("utf-8")

    for word in ("COM ポート", "ローカルポート", "ネットワークドライブ", "レート制限"):
        assert word in text, f"{word} が例示に含まれていない"


def test_any_resource_id_is_listed(tmp_path: Path) -> None:
    """資源 ID の種別で扱いを変えない。"""
    declare(tmp_path, "COM3", job="実機の教示", session="urcommander")
    declare(tmp_path, "\\\\nas\\share", job="バックアップ", session="malm")

    text = run_hook(tmp_path).decode("utf-8")

    assert "COM3" in text
    assert "nas" in text


def test_output_is_utf8(tmp_path: Path) -> None:
    """注入内容は常に UTF-8（cp932 で書くと読む側で化ける）。"""
    declare(tmp_path, "GPU0", job="日本語のジョブ名")

    raw = run_hook(tmp_path)

    assert "日本語のジョブ名" in raw.decode("utf-8")


def test_is_fast_enough_for_every_prompt(tmp_path: Path) -> None:
    """毎プロンプト走っても体感を損なわない。

    ``rb`` を起動する実装（実測 180ms）にしないための歯止めである。
    """
    declare(tmp_path, "GPU0", job="計測用")

    started = time.monotonic()
    run_hook(tmp_path)
    elapsed = time.monotonic() - started

    assert elapsed < BUDGET_S, f"{elapsed:.3f}s かかった"


# --- fail-open ------------------------------------------------------------------


def test_missing_board_directory_is_tolerated(tmp_path: Path) -> None:
    """掲示板がまだ無くても入力を妨げない。"""
    text = run_hook(tmp_path / "存在しない").decode("utf-8")

    assert "rb run" in text  # 規約だけは出る


@pytest.mark.parametrize("payload", ["", "{", "null", "[]", '{"resource": null}', "\x00\x01"])
def test_corrupt_entry_is_skipped(tmp_path: Path, payload: str) -> None:
    """壊れたエントリがあっても落ちない。読めるものだけ出す。"""
    entries = tmp_path / "board"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "壊れた.json").write_text(payload, encoding="utf-8")
    declare(tmp_path, "GPU0", job="正常なエントリ")

    text = run_hook(tmp_path).decode("utf-8")

    assert "正常なエントリ" in text


def test_does_not_depend_on_rb_being_installed(tmp_path: Path) -> None:
    """``rb`` が PATH に無くても動く。

    このフックは掲示板を直接読む。``rb`` の起動に依存すると、毎プロンプトの
    コストと、インストール状態への依存の両方を抱えることになる。
    """
    declare(tmp_path, "GPU0", job="rb 無しでも読める")

    text = run_hook(tmp_path, path=str(tmp_path / "何も無い")).decode("utf-8")

    assert "rb 無しでも読める" in text
