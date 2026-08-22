"""OS 依存の情報取得を隔離する層。

本ツールは Windows を第一級の対象とするが、CI は self-hosted の Linux ARM64 で回る。
プラットフォーム依存の処理をここに閉じ込めることで、掲示板・幽霊判定・命名といった
純粋なロジックをどの OS でも検証できるようにする。

いずれの関数も**失敗時は None を返し、例外を投げない**。判断材料が欠けたときは
呼び出し側が fail-open で通せるようにするためである。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta

from . import clock

_WINDOWS = sys.platform == "win32"
_DARWIN = sys.platform == "darwin"

# Windows API の定数
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

#: 起動からの経過ミリ秒を返すもの。**テストで差し替える。**
#:
#: Windows の実 API を呼ばずに、32 bit 相当の負値・巻き戻り・巨大値を与えて
#: :func:`boot_time` が壊れないことを検証するための注入点である。
#: CI は Linux で回るため、この経路は注入以外に検証手段が無い。
TickMillis = Callable[[], object]

#: 起動時刻（epoch 秒）を返すもの。**テストで差し替える。**
#:
#: macOS には ``/proc/uptime`` が無いため、そこだけ別経路になる。実機が無い環境
#: （CI は Linux、開発機は Windows）でも Darwin 経路を検証するための注入点である。
BootEpoch = Callable[[], object]

#: 起動からの経過時間として受け入れる上限（秒）。約 10 年。
#:
#: **実測値は sanity check を通す**。範囲外を通すと
#: 起動時刻がでたらめになり、幽霊判定で唯一「確定」とされている条件
#: （``since < 起動時刻``）が静かに壊れる。壊れ方は**稼働中の宣言が「再起動前の幽霊」
#: として退去される**という最悪の向きである。
MAX_UPTIME_S = 10.0 * 365.0 * 24.0 * 3600.0


def hostname() -> str:
    """このマシンのホスト名を返す。取得できなければ ``unknown-host``。"""
    try:
        name = socket.gethostname()
    except OSError:
        return "unknown-host"
    return (name or "unknown-host").strip().lower()


def session_id() -> str:
    """このセッションを識別する文字列。取れなければ空文字。

    **cwd だけでは所有を判定できない場面がある。** 同じリポジトリで 2 つのセッションを
    立てるのは日常であり（片方が実装、片方がレビュー等）、そのとき両者は cwd も
    ``session`` 名（cwd のベース名）も一致する。結果として、**自分の宣言を消したつもりで
    他セッションの宣言を消せてしまう**。

    ここで返すのは資源の情報ではなく**宣言者の身元**である。cwd や PID と同じ枠であり、
    資源の種別には一切依存しない。

    取れないときは空文字を返し、呼び出し側は従来どおり cwd で判定する。
    **無いことを理由に厳しくしない**。

    Returns
    -------
    str
        セッション ID。``RESOURCE_BROKER_SESSION_ID`` を優先し、無ければ Claude Code が
        渡す ``CLAUDE_CODE_SESSION_ID`` を使う。前者はテストと、Claude Code 以外から
        使うときの逃げ道である。
    """
    for name in ("RESOURCE_BROKER_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


#: ``sysctl -n kern.boottime`` の出力から秒を取り出す。
#: 例: ``{ sec = 1755300000, usec = 123456 } Sat Aug 16 09:00:00 2026``
#:
#: **直前に英字が無いことを要求する。** ``usec`` の中の ``sec`` に一致してしまい、
#: マイクロ秒を起動時刻として読む（テストで踏んだ）。実出力では ``sec`` が先に
#: 現れるため気づきにくい。
_BOOTTIME = re.compile(r"(?<![A-Za-z])sec\s*=\s*(\d+)")


def parse_boottime(text: object) -> int | None:
    """``kern.boottime`` の生出力から epoch 秒を取り出す。読めなければ None。

    **解析だけを分けてあるのは、subprocess 無しで検証できるようにするためである。**
    macOS の実機は CI にも開発機にも無い。
    """
    if not isinstance(text, str):
        return None
    match = _BOOTTIME.search(text)
    return int(match.group(1)) if match else None


def _darwin_boot_epoch() -> object:
    """macOS の起動時刻（epoch 秒）。取得できなければ None。

    **macOS には ``/proc/uptime`` が無い。** 塞がないと :func:`boot_time` が None を
    返し続け、幽霊判定 3 根拠のうち**唯一「確定的」な ``since < 起動時刻`` が
    静かに無効化される**。壊れ方が静かなのが最も悪い（再起動をまたいだ宣言が
    ``--force`` でしか消せなくなる）。

    ``sysctl`` は macOS に標準で入っている。無くても失敗は None に畳まれる。
    """
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_boottime(completed.stdout)


def _sane_boot_epoch(seconds: object) -> float | None:
    """起動時刻（epoch 秒）を sanity check にかける。範囲外なら None。

    :func:`_sane_uptime` と同じ理由である。**範囲外を通すと、生きている宣言を
    「再起動前の幽霊」として退去させうる。** 未来の起動時刻も弾く。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    value = float(seconds)
    if value != value or value <= 0.0:  # NaN と非正
        return None
    present = clock.now().timestamp()
    if value > present:  # 起動が未来にある
        return None
    if present - value > MAX_UPTIME_S:
        return None
    return value


def boot_time(
    *, tick_millis: TickMillis | None = None, boot_epoch: BootEpoch | None = None
) -> datetime | None:
    """このマシンが起動した時刻を返す。取得できなければ None。

    掲示板の幽霊判定に使う。エントリの ``since`` がこの時刻より前なら、
    そのエントリは再起動をまたいで残った幽霊であると**確定**できる
    （再起動で全 PID が無効かつ再利用されるため）。

    Parameters
    ----------
    tick_millis : callable, optional
        起動からの経過ミリ秒を返すもの。渡すと Windows 経路を強制する。
        テスト専用の注入点である（:data:`TickMillis`）。
    boot_epoch : callable, optional
        起動時刻（epoch 秒）を返すもの。渡すと macOS 経路を強制する。
        こちらもテスト専用の注入点である（:data:`BootEpoch`）。

    Returns
    -------
    datetime or None
        タイムゾーン付きの起動時刻。取得できなければ None。
    """
    if boot_epoch is not None or (_DARWIN and tick_millis is None):
        # macOS だけは起動時刻を直接取れる（経過時間からの逆算より素直である）。
        try:
            raw = (boot_epoch or _darwin_boot_epoch)()
        except Exception:  # noqa: BLE001 - この層は例外を出さない（モジュール docstring）
            return None
        seconds = _sane_boot_epoch(raw)
        return None if seconds is None else datetime.fromtimestamp(seconds).astimezone()

    uptime = _uptime_seconds(tick_millis=tick_millis)
    if uptime is None:
        return None
    return clock.now() - timedelta(seconds=uptime)


def _windows_tick_millis() -> object:
    """``GetTickCount64`` の生の戻り値（ミリ秒）。取得できなければ None。

    Notes
    -----
    **戻り値の型を必ず宣言する。** ``ctypes`` の既定の戻り値は ``c_int``
    （32 bit 符号付き）であり、64 bit を返す API をそのまま呼ぶと
    **稼働 24.9 日で負値、49.7 日で下位 32 bit へ巻き戻る**。巻き戻ると起動時刻が
    実際より大幅に新しく計算され、稼働中の宣言が「再起動前の幽霊」として退去される。
    幽霊判定の中で唯一「確定的」とされている条件が、静かに壊れる形である。

    ``GetTickCount64`` はスリープ・休止に費やした時間も含む（= 実時間の経過）。
    ``QueryUnbiasedInterruptTime`` はスリープ分を除くため、ここでは使わない。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        function = kernel32.GetTickCount64
        function.restype = ctypes.c_ulonglong
        function.argtypes = []
        return function()
    except Exception:  # noqa: BLE001 - 判定材料が無いだけ。呼び出し側は fail-open で通す
        return None


def _sane_uptime(seconds: object) -> float | None:
    """経過秒数を sanity check にかける。範囲外なら None（＝判定材料が無い）。

    **範囲外を通さない。** 実測値は壊れた値を返すことがある
    （実測: アイドル時の ``power.draw`` が 590.01 W と報告された）。
    ここで通すと起動時刻がでたらめになり、生きている宣言を退去させうる。
    判定できないときは「退けない」側（None）へ倒す。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    value = float(seconds)
    if value != value:  # NaN
        return None
    if value <= 0.0 or value > MAX_UPTIME_S:
        return None
    return value


def _uptime_seconds(*, tick_millis: TickMillis | None = None) -> float | None:
    """起動からの経過秒数を返す。取得できなければ None。

    Parameters
    ----------
    tick_millis : callable, optional
        起動からの経過ミリ秒を返すもの。渡すと Windows 経路を強制する。
    """
    if tick_millis is not None or _WINDOWS:
        try:
            millis = (tick_millis or _windows_tick_millis)()
        except Exception:  # noqa: BLE001 - この層は例外を出さない（モジュール docstring）
            return None
        if isinstance(millis, bool) or not isinstance(millis, (int, float)):
            return None
        return _sane_uptime(float(millis) / 1000.0)

    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            first = handle.readline().split()[0]
        return _sane_uptime(float(first))
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool | None:
    """指定 PID のプロセスが生きているかを返す。判定できなければ None。

    幽霊判定の**補助的な**材料である。長時間ジョブはプロセスを入れ替えながら
    継続することがあるため、これを単独の根拠にしてはならない。

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
