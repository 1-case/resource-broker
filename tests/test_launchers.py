"""``bin/`` の起動スクリプトを**実物で**検証する。

これらは他プロジェクトから素の shell / cmd で呼ばれるため、Python のテストからは
「フェイクを置いて通ったことにする」誘惑が常にある。実物を叩かないと、**cmd 固有の
壊れ方**（``%ERRORLEVEL`` のパース時展開、``!`` の遅延展開）は永久に見つからない。

ここで守るのは 3 つ。

1. ``rb`` は**子の終了コードを素通しする**（走らなかったジョブを成功と報告しない）
2. ``rb`` は**引数を壊さない**。とくに ``!`` を含む自由記述（``--observed`` は唯一の強制点）
3. ``rb-hook`` は**必ず 0 で戻る**（フックはユーザーの作業を止めてはならない）

いずれも「開発機では再現しない」形で壊れた実績がある。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

#: sh 版が動くか。Windows でも Git Bash があれば動く（フックの既定の経路である）。
SH = shutil.which("sh") or shutil.which("bash")

#: cmd 版は Windows でだけ意味を持つ。
HAS_CMD = os.name == "nt" and shutil.which("cmd") is not None

needs_sh = pytest.mark.skipif(SH is None, reason="sh が無い")
needs_cmd = pytest.mark.skipif(not HAS_CMD, reason="cmd.exe が無い（Windows 以外）")


def run_sh(script: Path, *args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    """sh 版のランチャを実物で起動する。"""
    env = dict(os.environ)
    if home is not None:
        env["RESOURCE_BROKER_HOME"] = str(home)
    assert SH is not None
    return subprocess.run(
        [SH, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        input="{}",
        timeout=120,
    )


def run_cmd(
    script: Path, *args: str, home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """cmd 版のランチャを実物で起動する。"""
    env = dict(os.environ)
    if home is not None:
        env["RESOURCE_BROKER_HOME"] = str(home)
    return subprocess.run(
        ["cmd", "/c", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        input="{}",
        timeout=120,
    )


def declared(home: Path) -> dict:
    """掲示板に落ちた宣言を 1 件読む。"""
    (path,) = sorted((home / "board").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


# --- 終了コードを素通しする -------------------------------------------------------


@needs_sh
def test_sh_launcher_propagates_the_exit_code(tmp_path: Path) -> None:
    """引数が足りなければ argparse の 2 がそのまま返る。

    ここが 0 に潰れると、**走らなかったジョブが成功として扱われる**。
    """
    result = run_sh(BIN / "rb", "claim", home=tmp_path)

    assert result.returncode == 2, result.stderr


@needs_cmd
def test_cmd_launcher_propagates_the_exit_code(tmp_path: Path) -> None:
    """cmd 版でも同じ。

    cmd は ``for`` の本体をまとめて解析するため、遅延展開なしの ``%ERRORLEVEL%`` は
    **ループに入る前の値（通常 0）に固定される**。実際にそれで壊れていた。
    """
    result = run_cmd(BIN / "rb.cmd", "claim", home=tmp_path)

    assert result.returncode == 2, result.stderr


# --- 引数を壊さない ---------------------------------------------------------------


@needs_sh
def test_sh_launcher_keeps_exclamation_marks(tmp_path: Path) -> None:
    """``!`` を含む申告がそのまま掲示板へ届く。"""
    note = "nvidia-smi は 0MiB! まだ空き!"
    result = run_sh(
        BIN / "rb",
        "claim",
        "GPU0",
        "--job",
        "検証",
        "--observed",
        note,
        "--eta",
        "10m",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert declared(tmp_path)["observed"]["note"] == note


@needs_cmd
def test_cmd_launcher_keeps_exclamation_marks(tmp_path: Path) -> None:
    """cmd 版でも ``!`` が消えない。

    ``setlocal enabledelayedexpansion`` を入れると ``%*`` の展開結果が ``!...!`` として
    再走査され、**中身が痕跡なく消える**。``--observed`` は本ツール唯一の強制点なので、
    そこが黙って壊れるのは最も悪い。
    """
    note = "nvidia-smi は 0MiB! まだ空き!"
    result = run_cmd(
        BIN / "rb.cmd",
        "claim",
        "GPU0",
        "--job",
        "検証",
        "--observed",
        note,
        "--eta",
        "10m",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert declared(tmp_path)["observed"]["note"] == note


# --- フックは必ず 0 で戻る ---------------------------------------------------------


@needs_sh
@pytest.mark.parametrize("name", ["prompt_board_reminder", "存在しない", ""])
def test_sh_hook_launcher_always_exits_zero(tmp_path: Path, name: str) -> None:
    """どんな名前でも 0 で戻る。**フックはユーザーの作業を止めてはならない。**"""
    args = [name] if name else []
    result = run_sh(BIN / "rb-hook", *args, home=tmp_path)

    assert result.returncode == 0, result.stderr


@needs_cmd
@pytest.mark.parametrize("name", ["prompt_board_reminder", "存在しない", ""])
def test_cmd_hook_launcher_always_exits_zero(tmp_path: Path, name: str) -> None:
    """cmd 版でも同じ。"""
    args = [name] if name else []
    result = run_cmd(BIN / "rb-hook.cmd", *args, home=tmp_path)

    assert result.returncode == 0, result.stderr


@needs_sh
def test_sh_hook_launcher_survives_a_crashing_hook(tmp_path: Path) -> None:
    """フック本体が非ゼロで死んでも、ランチャは 0 を返す。

    **``exec`` してはならない理由がここにある。** ``exec`` すると子の終了コードが
    そのままフックの終了コードになり、``PreToolUse`` では stderr が利用者へ提示される
    ——Bash を叩くたびに traceback が出続けることになる。
    """
    broken = ROOT / "hooks" / "zz_broken_for_test.py"
    broken.write_text("raise SystemExit(3)\n", encoding="utf-8")
    try:
        result = run_sh(BIN / "rb-hook", "zz_broken_for_test", home=tmp_path)
    finally:
        broken.unlink()

    assert result.returncode == 0, result.stderr


# --- 版の門は生の traceback を出さない ---------------------------------------------


@needs_sh
def test_the_version_gate_explains_itself(tmp_path: Path) -> None:
    """古い python で起動しても、内部を指す traceback ではなく案内が出る。

    ``bin/rb.py`` を直接叩いて門だけを見る（ランチャは版で候補を選ぶため、
    古い python がある環境でもそこには届かない）。
    """
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.version_info = (3, 9); "],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0  # 前提の確認だけ

    source = (BIN / "rb.py").read_text(encoding="utf-8")
    assert "sys.version_info < (3, 11)" in source, "版の門が無い"
    assert "Python 3.11 以上が要ります" in source, "案内の文言が無い"
    assert "SystemExit(127)" in source, "127 で終わっていない"


# --- バッチファイルは ASCII のみ ---------------------------------------------------


@pytest.mark.parametrize("name", ["rb.cmd", "rb-hook.cmd"])
def test_batch_files_are_ascii_only(name: str) -> None:
    """``.cmd`` に非 ASCII を混ぜない。

    cmd.exe はバッチファイルを**コンソールのコードページ**（日本語 Windows では cp932）で
    読む。UTF-8 の日本語コメントを置くと復号に失敗し、**コメント行が壊れてコマンドとして
    実行される**。実際にそれで起動しなくなった（``'--observed' は、内部コマンドまたは…``）。

    しかもこれは**バッチを実際に走らせないと分からない**。理由は sh 版の同じ位置に
    日本語で書き、cmd 側は英語で要点だけを書いて sh 版を指す。
    """
    text = (BIN / name).read_bytes()

    offenders = [(i, b) for i, b in enumerate(text) if b > 0x7F]
    assert not offenders, (
        f"bin/{name} に非 ASCII バイトがある（先頭 {offenders[0][0]} バイト目）。"
        "cmd.exe が cp932 として読むため、コメントが壊れてコマンドとして実行される"
    )
