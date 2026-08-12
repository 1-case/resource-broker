"""OS 依存の情報取得を隔離する層。

本ツールは Windows を第一級の対象とするが、CI は self-hosted の Linux ARM64 で回る。
プラットフォーム依存の処理をここに閉じ込めることで、掲示板・幽霊判定・命名といった
純粋なロジックをどの OS でも検証できるようにする。

いずれの関数も**失敗時は None を返し、例外を投げない**。判断材料が欠けたときは
呼び出し側が fail-open で通せるようにするためである。
"""

from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timedelta

from . import clock

_WINDOWS = sys.platform == "win32"

# Windows API の定数
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def hostname() -> str:
    """このマシンのホスト名を返す。取得できなければ ``unknown-host``。"""
    try:
        name = socket.gethostname()
    except OSError:
        return "unknown-host"
    return (name or "unknown-host").strip().lower()


def boot_time() -> datetime | None:
    """このマシンが起動した時刻を返す。取得できなければ None。

    掲示板の幽霊判定に使う。エントリの ``since`` がこの時刻より前なら、
    そのエントリは再起動をまたいで残った幽霊であると**確定**できる
    （再起動で全 PID が無効かつ再利用されるため）。

    Returns
    -------
    datetime or None
        タイムゾーン付きの起動時刻。取得できなければ None。
    """
    uptime = _uptime_seconds()
    if uptime is None:
        return None
    return clock.now() - timedelta(seconds=uptime)


def _uptime_seconds() -> float | None:
    """起動からの経過秒数を返す。取得できなければ None。"""
    if _WINDOWS:
        try:
            import ctypes

            # GetTickCount64 はスリープ・休止に費やした時間も含む（= 実時間の経過）。
            # QueryUnbiasedInterruptTime はスリープ分を除くため、ここでは使わない。
            millis = ctypes.windll.kernel32.GetTickCount64()  # type: ignore[attr-defined]
        except Exception:
            return None
        if not isinstance(millis, int) or millis <= 0:
            return None
        return millis / 1000.0

    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            first = handle.readline().split()[0]
        return float(first)
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool | None:
    """指定 PID のプロセスが生きているかを返す。判定できなければ None。

    幽霊判定の**補助的な**材料である。長時間ジョブはプロセスを入れ替えながら
    継続することがあるため、これを単独の根拠にしてはならない
    （CLAUDE.md「Liveness Judgment」）。

    Parameters
    ----------
    pid : int or None
        調べる PID。

    Returns
    -------
    bool or None
        生存していれば True、していなければ False、判定できなければ None。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None

    if _WINDOWS:
        return _pid_alive_windows(pid)

    # POSIX ではシグナル 0 の送信が存在確認になる。
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 他ユーザーのプロセス。存在はしている。
        return True
    except OSError:
        return None
    return True


def _pid_alive_windows(pid: int) -> bool | None:
    """Windows での PID 生存確認。

    Notes
    -----
    Windows で ``os.kill(pid, 0)`` を使ってはならない。Python の実装では
    CTRL_C_EVENT / CTRL_BREAK_EVENT 以外のシグナル値が渡されると
    ``TerminateProcess`` が呼ばれ、**対象プロセスを無条件に終了させてしまう**。
    存在確認のつもりが他セッションのジョブを殺すことになる。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            code = ctypes.get_last_error() or kernel32.GetLastError()
            if code == _ERROR_INVALID_PARAMETER:
                return False  # そのような PID は存在しない
            if code == _ERROR_ACCESS_DENIED:
                return True  # 存在するが権限が足りない
            return None
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def board_root() -> str:
    """掲示板を置くディレクトリのパスを返す。

    マシン全体で 1 箇所に置く。全アセットから参照するため、プロジェクト配下には置かない。
    環境変数 ``RESOURCE_BROKER_HOME`` があればそれを優先する（テストと検証で差し替えるため）。
    """
    override = os.environ.get("RESOURCE_BROKER_HOME")
    if override:
        return override
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return os.path.join(local_app_data, "resource-broker")
    return os.path.join(os.path.expanduser("~"), ".resource-broker")
