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
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from resource_broker.board import Board, build_entry
from resource_broker.naming import normalize

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


@pytest.fixture
def hook_tree(tmp_path: Path) -> Path:
    """``bin/rb-hook`` と空の ``hooks/`` だけを持つ木を tmp へ作る。

    ランチャは自分の在り処から ``../hooks`` を決めるので、これで**作業ツリーへ 1 バイトも
    書かずに**フックの置き場を差し替えられる。実物のランチャを叩く方針は変えない。
    """
    (tmp_path / "bin").mkdir()
    (tmp_path / "hooks").mkdir()
    for name in ("rb-hook", "rb-hook.cmd"):
        shutil.copy2(BIN / name, tmp_path / "bin" / name)
    return tmp_path


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


#: フックの置き場から外へ出ようとする名前。``rb-hook`` は名前を**そのまま**パスへ
#: 連結するので、ここを検証しないと ``hooks/`` の外の ``.py`` が走る。
TRAVERSAL = ["../escaped", "..\\escaped", "sub/escaped"]


@needs_sh
@pytest.mark.parametrize("name", TRAVERSAL)
def test_sh_hook_launcher_refuses_to_leave_the_hooks_directory(hook_tree: Path, name: str) -> None:
    """フックの置き場の外にある ``.py`` は、**存在しても**走らない。

    ``存在しない`` を渡すだけでは、名前の検証を外しても ``[ -f ]`` が拾って 0 になる。
    **実在するファイルを指す名前**で確かめないと、検証が消えたことに気づけない。
    """
    (hook_tree / "escaped.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('escaped.marker').write_text('ran')\n",
        encoding="utf-8",
    )
    (hook_tree / "hooks" / "sub").mkdir(exist_ok=True)
    (hook_tree / "hooks" / "sub" / "escaped.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).parent.parent.parent.joinpath('escaped.marker').write_text('ran')\n",
        encoding="utf-8",
    )

    result = run_sh(hook_tree / "bin" / "rb-hook", name, home=hook_tree)

    assert result.returncode == 0, result.stderr
    assert not (hook_tree / "escaped.marker").exists(), f"{name} で置き場の外が走った"


@needs_cmd
@pytest.mark.parametrize("name", TRAVERSAL)
def test_cmd_hook_launcher_refuses_to_leave_the_hooks_directory(
    hook_tree: Path, name: str
) -> None:
    """cmd 版でも同じ。**sh 版だけ検証があると、Windows でだけ穴が開く。**"""
    (hook_tree / "escaped.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('escaped.marker').write_text('ran')\n",
        encoding="utf-8",
    )
    (hook_tree / "hooks" / "sub").mkdir(exist_ok=True)
    (hook_tree / "hooks" / "sub" / "escaped.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).parent.parent.parent.joinpath('escaped.marker').write_text('ran')\n",
        encoding="utf-8",
    )

    result = run_cmd(hook_tree / "bin" / "rb-hook.cmd", name, home=hook_tree)

    assert result.returncode == 0, result.stderr
    assert not (hook_tree / "escaped.marker").exists(), f"{name} で置き場の外が走った"


@needs_sh
def test_sh_hook_launcher_survives_a_crashing_hook(hook_tree: Path) -> None:
    """フック本体が非ゼロで死んでも、ランチャは 0 を返す。

    **``exec`` してはならない理由がここにある。** ``exec`` すると子の終了コードが
    そのままフックの終了コードになり、``PreToolUse`` では stderr が利用者へ提示される
    ——Bash を叩くたびに traceback が出続けることになる。
    """
    (hook_tree / "hooks" / "zz_broken.py").write_text("raise SystemExit(3)\n", encoding="utf-8")

    result = run_sh(hook_tree / "bin" / "rb-hook", "zz_broken", home=hook_tree)

    assert result.returncode == 0, result.stderr


# --- ランチャの正常系（壊れたら沈黙するので、ここだけは動作で確かめる） -------------


@needs_sh
def test_sh_hook_launcher_actually_runs_the_hook(tmp_path: Path) -> None:
    """フックが**実際に出力する**ことを確かめる。

    ``rb-hook`` は必ず 0 で戻る設計なので、**壊れたときの症状は必ず沈黙**である。
    終了コードだけを見るテストは、ランチャを空ファイルにしても全部通る。配布層は
    実際に cp932 でそうやって壊れた。ここだけは「出た」ことを見る。
    """
    board = Board(tmp_path)
    assert board.try_claim(build_entry(normalize("GPU0"), job="E059 eval", session="folnet"))

    result = run_sh(BIN / "rb-hook", "prompt_board_reminder", home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "GPU0" in result.stdout, result.stdout


@needs_cmd
def test_cmd_hook_launcher_actually_runs_the_hook(tmp_path: Path) -> None:
    """cmd 版でも同じ。**Windows でだけ沈黙する壊れ方を許さない。**"""
    board = Board(tmp_path)
    assert board.try_claim(build_entry(normalize("GPU0"), job="E059 eval", session="folnet"))

    result = run_cmd(BIN / "rb-hook.cmd", "prompt_board_reminder", home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "GPU0" in result.stdout, result.stdout


def test_every_hook_named_in_hooks_json_exists_and_is_launchable() -> None:
    """``hooks.json`` の名前・``hooks/*.py`` の実ファイル・ランチャの制約が一致している。

    フックを 1 つ改名すると ``hooks.json`` の指す先が消え、``rb-hook`` は ``[ -f ]`` で
    0 を返して**沈黙する**。三者がずれたことに気づく手段が他に無い。
    """
    spec = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in spec["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands, spec

    for command in commands:
        name = command.rsplit(" ", 1)[-1]
        assert re.fullmatch(r"[a-z0-9_]+", name), f"{name} はランチャの制約に合わない"
        assert (ROOT / "hooks" / f"{name}.py").is_file(), f"hooks/{name}.py が無い"


def test_every_hook_declares_an_upper_bound_on_its_own_runtime() -> None:
    """フックに上限時間が書いてある。

    ``PreToolUse`` が返らないと**全 Bash 呼び出しが待たされる**。``guard.json`` に
    指数時間の正規表現を 1 本置けば起こる。上限は 1 行で確定できる。
    """
    spec = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    for groups in spec["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert isinstance(hook.get("timeout"), int), hook


# --- 版の門は生の traceback を出さない ---------------------------------------------


def test_the_version_gate_explains_itself() -> None:
    """古い python で起動しても、内部を指す traceback ではなく案内が出る。

    ``bin/rb.py`` を直接叩いて門だけを見る（ランチャは版で候補を選ぶため、
    古い python がある環境でもそこには届かない）。
    """
    # **ソースを grep するだけでは門が消えたことに気づけない。** 実際に古い版だと
    # 見せかけて走らせ、127 と案内が出ることを確かめる。
    probe = (
        "import sys\n"
        "sys.version_info = (3, 9, 0)\n"
        "path = sys.argv[1]\n"
        "src = open(path, encoding='utf-8').read()\n"
        "exec(compile(src, path, 'exec'), {'__name__': '__main__', '__file__': path})\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(BIN / "rb.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert result.returncode == 127, f"門が効いていない: rc={result.returncode}"
    assert "Python 3.11" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


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
