"""CLI が実際に英語で話すことを守る番人（issue #13 第 2 段。issue #26 と同じ観点）。

``tests/conftest.py`` の自動フィクスチャは既定を ``ja`` に固定する。**それだけで
終えると、英語で出力したときに壊れていても誰も気づけない**——``tests/test_hook_i18n.py``
と ``tests/test_hook_language_switch.py`` の関係と同じで、単体レベルの番人
（``tests/test_i18n_guard.py``）が「訳が欠けたら日本語で動く」を保証していても、
**実際に英語を選んだときに出力が英語になっていること**は別に確かめる必要がある。

ここでは ``RESOURCE_BROKER_LANG=en`` を明示し、次を確かめる。

1. 固定文言が実際に英語になること
2. **宣言の自由記述（--job / --observed / --sharing）は訳されず、書いたまま出ること**
   （``docs/DESIGN.md``「方針」——訳す対象ではないのは表記ではなく中身だから）
3. 日本語の固定文言が混ざっていないこと（言語が中途半端に切り替わっていない）
4. ``--help`` の argparse 由来の文言も英語になること
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resource_broker.cli import main

#: 自由記述に混ぜる、機械翻訳や定型文言とは衝突しない目印つきの日本語文字列。
UNTRANSLATED_JOB = "日本語ジョブ名E059"
UNTRANSLATED_OBSERVED = "観測メモそのまま"
UNTRANSLATED_SHARING = "共有可（要連絡）そのまま"


def run(tmp_path: Path, *args: str) -> int:
    return main(["--home", str(tmp_path), *args])


def claim(tmp_path: Path, resource: str, *extra: str) -> int:
    return run(
        tmp_path,
        "claim",
        resource,
        "--job",
        UNTRANSLATED_JOB,
        "--observed",
        UNTRANSLATED_OBSERVED,
        "--eta",
        "30m",
        *extra,
    )


@pytest.fixture(autouse=True)
def _speak_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """このファイルの全テストで ``RESOURCE_BROKER_LANG=en`` を明示する。"""
    monkeypatch.setenv("RESOURCE_BROKER_LANG", "en")


def test_status_speaks_english_on_an_empty_board(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "The board is empty" in out
    assert "declare it with rb claim" in out
    assert "掲示板は空です" not in out


def test_claim_speaks_english_and_keeps_free_text_untranslated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert claim(tmp_path, "GPU0") == 0
    out = capsys.readouterr().out
    assert "Declared:" in out
    assert UNTRANSLATED_JOB in out
    assert "宣言しました" not in out


def test_claim_refusal_speaks_english_and_keeps_free_text_untranslated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert claim(tmp_path, "GPU0", "--sharing", UNTRANSLATED_SHARING) == 0
    capsys.readouterr()

    code = claim(tmp_path, "GPU0", "--job", "second job")
    err = capsys.readouterr().err

    assert code == 1
    assert "is in use" in err
    assert "Use --share" in err
    assert UNTRANSLATED_JOB in err  # 先に取った側の job（自由記述）
    assert UNTRANSLATED_SHARING in err
    assert "は使用中です" not in err
    assert "並んで使うなら" not in err


def test_release_speaks_english(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert claim(tmp_path, "GPU0") == 0
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0") == 0
    out = capsys.readouterr().out
    assert "Released:" in out
    assert "解放しました" not in out


def test_release_of_nothing_speaks_english(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "release", "GPU0") == 0
    out = capsys.readouterr().out
    assert "There were no declarations" in out
    assert "宣言はありませんでした" not in out


def test_help_speaks_english(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # ``main()`` は argparse の ``SystemExit`` をここで catch して終了コードへ変換する
    # （``--help`` や引数不備の経路）。呼び出し側からは通常の戻り値として見える。
    assert run(tmp_path, "--help") == 0
    out = capsys.readouterr().out
    assert "board that shares finite-resource usage" in out
    assert "並行する Claude Code セッション" not in out


def test_claim_help_speaks_english(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, "claim", "--help") == 0
    out = capsys.readouterr().out
    assert "What you are doing" in out
    assert "何をするか" not in out
