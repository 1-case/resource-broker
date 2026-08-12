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
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import clock, naming

#: ログを置くディレクトリ名（掲示板のルート直下）。
#:
#: 掲示板と同じくマシン全体で 1 箇所に置く。**他セッションが読めることが要件**であり、
#: プロジェクト配下に置くと掲示板の ``log`` を辿った先が読めない場合がある。
LOG_DIR = "logs"

#: 1 ジョブあたりのログの上限（バイト）。超えた分は捨てる。
#:
#: ログは掲示板・監査ログと**同じルート**に置く。出力の多い長時間ジョブ 1 つで
#: ディスクを埋めると、掲示板も監査ログも書けなくなる。そうなると全セッションが
#: fail-open で通り、**掲示板が無いのと同じ状態で資源を取り合う**。
#: 上限に達してもジョブは止めない（fail-open）。捨てたことは 1 行残す。
MAX_LOG_BYTES = 128 * 1024 * 1024

#: ログを残す日数。``rb run`` の起動時にこれより古いものを掃除する。
#:
#: 掃除の失敗は無視する。**掃除でジョブを止めない**。
LOG_RETENTION_DAYS = 7

#: 子から読み取る 1 回あたりのバイト数。
_READ_CHUNK = 65536

#: 子プロセスを起動して終了コードを返すもの。
Spawn = Callable[[list[str], Path, Mapping[str, str]], int]

#: 子プロセスが中断に応じるのを待つ秒数。過ぎたら強制終了する。
TERMINATE_TIMEOUT_S = 10.0

#: プロセスグループが静まったかを確かめる間隔（秒）。
GROUP_POLL_S = 0.1

#: プロセスツリーを終了させるもの。``(pid, force) -> 成否``。テストで差し替える。
KillTree = Callable[[int, bool], bool]

#: プロセスグループの生存を調べるもの。``pgid -> True / False / None``。
#:
#: None は「判定できない」であり、**生存とはみなさない**（Windows には同じ概念が無く、
#: ``taskkill /T`` が木ごと落とすため直接の子の終了で足りる）。テストで差し替える。
GroupAlive = Callable[[int], bool | None]

#: シグナル番号。**モジュールの import 時に解決する**。
#:
#: Windows の ``signal`` には ``SIGKILL`` が無い。POSIX 側の分岐の中で参照しても、
#: テストからその関数を直接呼んだ瞬間に ``AttributeError`` になる。
_SIGTERM = int(getattr(signal, "SIGTERM", 15))
_SIGKILL = int(getattr(signal, "SIGKILL", 9))

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


def prune_own_logs(root: Path, *, max_age_days: float = LOG_RETENTION_DAYS) -> int:
    """**本ツールが作ったログ置き場だけ**を掃除する。

    掲示板のルート直下の ``logs/`` に限る。``--log`` で指定された場所は触らない。
    そこは利用者のディレクトリであり、本ツールが作っていない ``*.log`` が同居しうる。
    掃除の対象を「書き込み先」にすると、掲示板を守るための掃除が**管理外のファイルを消す**。
    """
    return prune_old_logs(Path(root) / LOG_DIR, max_age_days=max_age_days)


def prune_old_logs(directory: Path, *, max_age_days: float = LOG_RETENTION_DAYS) -> int:
    """古いログを掃除する。消せた件数を返す。

    生きているジョブのログは書き込みが続いているので mtime が新しく、対象にならない。

    **失敗は全て無視する。** 掃除は付随的な仕事であり、ここで例外を出すと
    「掃除できないからジョブが走らない」という本末転倒が起きる。
    """
    cutoff = time.time() - max_age_days * 86400.0
    try:
        paths = list(directory.glob("*.log"))
    except OSError:
        return 0

    removed = 0
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _pump(source: object, sink: object, limit: int) -> None:
    """子の出力をログへ写す。上限を超えたら**読むのは続け、書くのをやめる**。

    **読むのをやめてはならない。** パイプが詰まると子が書き込みでブロックし、
    「ログの上限」がジョブの停止に化ける。本ツールがユーザーの作業を止めてよい場面は
    無い（CLAUDE.md「Fail-Open」）。

    捨て始めたことは 1 行残す。黙って捨てると、読む側は「ここでジョブが止まった」と読む
    （CLAUDE.md「Silence Is Not Success」）。
    """
    written = 0
    truncated = False
    try:
        while True:
            chunk = source.read1(_READ_CHUNK)  # type: ignore[attr-defined]
            if not chunk:
                break
            if truncated:
                continue  # 読み捨てる。子をブロックさせない
            if written + len(chunk) <= limit:
                sink.write(chunk)  # type: ignore[attr-defined]
                written += len(chunk)
            else:
                room = max(0, limit - written)
                if room:
                    sink.write(chunk[:room])  # type: ignore[attr-defined]
                    written += room
                notice = (
                    f"\n=== {clock.now_iso()} ログが上限（{limit} バイト）に達したため、"
                    "以降の出力を破棄した（ジョブは継続中）\n"
                )
                sink.write(notice.encode("utf-8", errors="replace"))  # type: ignore[attr-defined]
                truncated = True
            sink.flush()  # type: ignore[attr-defined]
    except (OSError, ValueError):
        pass  # ログが書けなくなってもジョブは止めない
    finally:
        try:
            source.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass


def default_spawn(argv: list[str], log_path: Path, env: Mapping[str, str]) -> int:
    """子プロセスを起動し、stdout/stderr をログへ落として終了を待つ。

    中断（Ctrl+C 等）を受けたら子を終了させてから送出しなおす。
    ラッパーだけ抜けて子が生き残ると、掲示板から消えた資源を掴んだままの
    プロセスが残り、最も検出しにくい形の不整合になる。

    出力は**子の fd を直接ログへ向けず**、パイプを経由して写す。直結すると
    書き込み量に蓋ができない。ログは掲示板・監査ログと同じルートにあるため、
    出力の多いジョブ 1 つでディスクが埋まると**全セッションが掲示板を失う**。

    Returns
    -------
    int
        子プロセスの終了コード。
    """
    # **ここで掃除をしない。** ``--log`` で場所を指定できるので、掃除を「書き込み先の
    # ディレクトリ」に対して行うと、利用者のプロジェクト配下にある**無関係な `*.log` を消す**。
    # 掃除の対象は本ツールが作った置き場に限る（呼び出し側が `prune_old_logs` を呼ぶ）。
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"=== {clock.now_iso()} rb run: {subprocess.list2cmdline(argv)}\n"

    with log_path.open("ab") as stream:
        stream.write(header.encode("utf-8", errors="replace"))
        stream.flush()
        # POSIX では新しいプロセスグループにして、中断時に子孫までシグナルを届かせる。
        # Windows は taskkill /T で木ごと落とすのでここでは何もしない。
        extra: dict[str, object] = {} if sys.platform == "win32" else {"start_new_session": True}
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env),
            **extra,  # type: ignore[arg-type]
        )
        pump = threading.Thread(
            target=_pump, args=(process.stdout, stream, MAX_LOG_BYTES), daemon=True
        )
        pump.start()
        try:
            return process.wait()
        except BaseException:
            _stop(process)
            raise
        finally:
            # 子が終わってもパイプに残りがある。書き切ってから戻る。
            pump.join(timeout=TERMINATE_TIMEOUT_S)


def _stop(
    process: subprocess.Popen[bytes],
    *,
    kill_tree: KillTree | None = None,
    group_alive: GroupAlive | None = None,
    timeout_s: float = TERMINATE_TIMEOUT_S,
) -> None:
    """子プロセスを**その子孫ごと**止める。応じなければ強制終了へ昇格する。

    直接の子だけを殺しても足りない。推奨している形が
    ``rb run ... -- uv run python scripts/train.py`` であり、直接の子は ``uv`` で、
    資源を掴むのは孫の ``python`` である。``uv`` だけ殺すと孫が生き残り、
    ``finally`` が掲示板から宣言を消す。**掲示板は空・資源は掴まれたまま**という
    最も検出しにくい不整合ができる。

    **昇格の判断を「直接の子が終わったか」で行わない。** ``uv`` のような直接の子が
    ``SIGTERM`` に素直に応じると ``process.wait()`` は成功して戻るため、そこで
    return すると**孫が ``SIGTERM`` を無視して資源を掴んだまま残っていても昇格しない**。
    見るのはプロセスグループ全体の生存であり、猶予を過ぎたらグループへ ``SIGKILL``
    を送る。

    Parameters
    ----------
    kill_tree : callable, optional
        プロセスツリーを終了させるもの。テストで差し替える
        （実プロセスを起動せずに昇格の順序を検証するため）。
    group_alive : callable, optional
        プロセスグループの生存を調べるもの。同じくテストで差し替える。
    """
    killer: KillTree = kill_tree or _kill_tree
    watcher: GroupAlive = group_alive or _group_alive

    if killer(process.pid, False):
        if _wait_for_group(process, watcher, timeout_s):
            return
        # 直接の子か孫が SIGTERM を無視して残っている。強制終了へ昇格する。
        if killer(process.pid, True):
            _wait_for_group(process, watcher, timeout_s)
            return

    try:
        process.terminate()
        if not _wait_quietly(process, timeout_s):
            process.kill()
            _wait_quietly(process, timeout_s)
    except OSError:
        pass


def _wait_quietly(process: subprocess.Popen[bytes], timeout_s: float) -> bool:
    """子の終了を待つ。時間内に終われば True。"""
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    return True


def _wait_for_group(
    process: subprocess.Popen[bytes], group_alive: GroupAlive, timeout_s: float
) -> bool:
    """直接の子と**そのプロセスグループ全体**が静まるのを待つ。静まれば True。

    ``process.wait()`` だけでは足りない理由は :func:`_stop` の docstring にある。
    生存を判定できない（None）場合は静まったものとして扱う。Windows には
    プロセスグループの概念が無く、``taskkill /T`` が木ごと落とすためである。
    ここで「判定できない」を「生きている」に倒すと、Windows で毎回 ``SIGKILL`` 相当へ
    昇格し、素直に終わろうとしている子を強制終了することになる。
    """
    deadline = time.monotonic() + timeout_s
    if not _wait_quietly(process, timeout_s):
        return False

    while True:
        if group_alive(process.pid) is not True:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(GROUP_POLL_S)


def _group_alive(pid: int) -> bool | None:
    """``pid`` のプロセスグループに生きているプロセスが居るか。判定できなければ None。

    Notes
    -----
    ``os.getpgid`` を経由しない。**直接の子を ``wait()`` で回収した後は ``getpgid`` が
    失敗する**のに対し、プロセスグループはメンバーが 1 つでも残っていれば存在する。
    経由すると「孫だけが生き残っている」という、まさに見たい状態が
    「グループは無い」に化ける。子は ``start_new_session=True`` で起動しているので
    ``pgid == 子の PID`` である。
    """
    if sys.platform == "win32":
        return None
    try:
        os.killpg(pid, 0)  # シグナル 0 は送らずに存在確認だけを行う
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 他ユーザーのプロセスが居る＝生きている
    except (OSError, AttributeError):
        return None
    return True


def _kill_tree(pid: int, force: bool = False) -> bool:
    """プロセスツリーごと終了させる。できなければ False。

    Windows は ``taskkill /T``、POSIX はプロセスグループへのシグナルで子孫まで届かせる。
    どちらも失敗したら呼び出し側が単体終了へ退避する（何もしないよりはよい）。

    Parameters
    ----------
    force : bool
        ``True`` なら無視できないシグナルを送る。POSIX では ``SIGKILL``。
        Windows の ``taskkill /F`` は最初から強制なので区別が無い。
    """
    if sys.platform == "win32":
        return _kill_tree_windows(pid)
    return _kill_tree_posix(pid, force)


def _kill_tree_windows(pid: int) -> bool:
    """Windows でプロセスツリーを終了させる。``/F`` は常に強制である。"""
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=TERMINATE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _kill_tree_posix(pid: int, force: bool) -> bool:
    """POSIX でプロセスグループごと終了させる。

    ``getpgid`` が失敗したら PID を pgid として使う。**直接の子を回収した後は
    ``getpgid`` が失敗する**が、孫が残っていればグループはまだ存在する。ここで
    諦めると、資源を掴んでいる孫へ ``SIGKILL`` が届かない。子は
    ``start_new_session=True`` で起動しているので ``pgid == 子の PID`` である。
    """
    try:
        try:
            pgid = os.getpgid(pid)
        except (OSError, AttributeError):
            pgid = pid
        os.killpg(pgid, _SIGKILL if force else _SIGTERM)
    except (OSError, AttributeError):
        return False
    return True


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
