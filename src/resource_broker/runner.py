"""子プロセスの起動とログの強制。ラッパー（``rb run``）の資源に触れない部分。

ラッパーの目的は**人が忘れても成立させる**ことである。手動運用の検証で、
ジョブ完了から解放まで 77 秒のあいだ掲示板が嘘をつく状態が実際に起きた。
解放を意思に頼るのをやめ、``finally`` で機械的に外す。

ここには資源の知識も、コマンドの種類の知識も置かない。バッファリングの無効化は
``PYTHONUNBUFFERED`` を子の環境へ入れることで行い、**コマンド行を解釈しない**
（``python`` を見つけて ``-u`` を挿す実装は、`uv run python` や `py -3` を取りこぼす）。

テストでは ``spawn`` を差し替えて実プロセスを起動せずに検証できる
（CLAUDE.md「Testing Constraints」）。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import clock, naming

#: ログを置くディレクトリ名（掲示板のルート直下）。
#:
#: 掲示板と同じくマシン全体で 1 箇所に置く。**他セッションが読めることが要件**であり、
#: プロジェクト配下に置くと掲示板の ``log`` を辿った先が読めない場合がある。
LOG_DIR = "logs"

#: 子プロセスを起動して終了コードを返すもの。
Spawn = Callable[[list[str], Path, Mapping[str, str]], int]

#: 子プロセスが中断に応じるのを待つ秒数。過ぎたら強制終了する。
TERMINATE_TIMEOUT_S = 10.0

#: コマンドが見つからなかった（シェルの慣習に合わせる）。
EXIT_COMMAND_NOT_FOUND = 127

#: コマンドを起動できなかった。
EXIT_CANNOT_EXECUTE = 126


def build_log_path(root: Path, resource_id: str) -> Path:
    """既定のログ出力先を組み立てる。

    ファイル名は資源 ID を安全化したものに時刻を付ける。同じ資源を続けて使っても
    前回のログを上書きしない。

    Parameters
    ----------
    root : Path
        掲示板のルート。
    resource_id : str
        正規化済みの資源 ID。

    Returns
    -------
    Path
        ログファイルのパス。ディレクトリはまだ作らない。
    """
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    return Path(root) / LOG_DIR / f"{naming.safe_filename(resource_id)}-{stamp}.log"


def child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """子プロセスへ渡す環境変数を作る。

    ``PYTHONUNBUFFERED`` を立てて出力のバッファリングを無効にする。
    掲示板の ``log`` は「他セッションが進捗を読むための観測点」であり、
    バッファに溜まったまま出てこないログは観測点として役に立たない。
    """
    env = dict(os.environ if base is None else base)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def default_spawn(argv: list[str], log_path: Path, env: Mapping[str, str]) -> int:
    """子プロセスを起動し、stdout/stderr をログへ落として終了を待つ。

    中断（Ctrl+C 等）を受けたら子を終了させてから送出しなおす。
    ラッパーだけ抜けて子が生き残ると、掲示板から消えた資源を掴んだままの
    プロセスが残り、最も検出しにくい形の不整合になる。

    Returns
    -------
    int
        子プロセスの終了コード。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"=== {clock.now_iso()} rb run: {subprocess.list2cmdline(argv)}\n"

    with log_path.open("ab") as stream:
        stream.write(header.encode("utf-8", errors="replace"))
        stream.flush()
        process = subprocess.Popen(
            argv,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=dict(env),
        )
        try:
            return process.wait()
        except BaseException:
            _stop(process)
            raise


def _stop(process: subprocess.Popen[bytes]) -> None:
    """子プロセスを止める。応じなければ強制終了する。"""
    try:
        process.terminate()
        process.wait(timeout=TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def execute(
    argv: Sequence[str],
    log_path: Path,
    *,
    spawn: Spawn = default_spawn,
    env: Mapping[str, str] | None = None,
) -> int:
    """コマンドを実行して終了コードを返す。

    起動そのものに失敗した場合も**例外を外に出さない**。ただし **0 も返さない**。
    fail-open は「本ツールが壊れてもユーザーの資源アクセスを止めない」という原則であって、
    「走らなかったジョブを成功と報告してよい」という意味ではない。ここで 0 を返すと、
    ラッパーの故障がジョブの成功に化ける。

    中断（``KeyboardInterrupt`` 等の ``BaseException``）はそのまま送出する。
    呼び出し側が ``finally`` で解放したうえで中断として扱う。
    """
    try:
        return int(spawn(list(argv), Path(log_path), child_environment(env)))
    except FileNotFoundError:
        return EXIT_COMMAND_NOT_FOUND
    except Exception:  # noqa: BLE001 - 起動できなかったことを終了コードで表す
        return EXIT_CANNOT_EXECUTE
