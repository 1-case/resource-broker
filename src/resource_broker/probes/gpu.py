"""NVIDIA GPU の実測プローブ（nvidia-smi）。

Windows/WDDM では ``nvidia-smi`` が**プロセス別 VRAM を報告しない**
（``used_gpu_memory`` が ``[N/A]`` になる）。したがって「誰がどれだけ使っているか」は
実測できず、帰属は掲示板の宣言に頼る。ここで実測できるのは
「compute プロセスが 1 つでもいるか」と「総使用量・使用率」だけである。

``nvidia-smi`` は dGPU が低電力状態にあるとき初回応答が遅い（実測で約 2 秒）。
必ずタイムアウトを付けて呼び、ブロックしても判定不能として返す。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from .base import Observation

# 実測値の妥当な範囲。範囲外は「センサが無効」とみなして detail から落とす
# （アイドル時に power.draw が 590 W と報告された実績がある）。
_MAX_VRAM_MIB = 1_000_000
_MAX_UTIL = 100

CommandRunner = Callable[[list[str], float], tuple[int, str]]


def _default_runner(args: list[str], timeout_s: float) -> tuple[int, str]:
    """コマンドを実行し ``(returncode, stdout)`` を返す。失敗は負の returncode で表す。"""
    try:
        # 引数は固定でシェルを介さない（ユーザー入力を渡さない）
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -2, ""
    except OSError:
        return -3, ""
    return completed.returncode, completed.stdout or ""


@dataclass
class NvidiaSmiProbe:
    """``nvidia-smi`` を使って GPU の使用状況を実測する。

    Parameters
    ----------
    timeout_s : float
        1 回の呼び出しのタイムアウト秒数。
    runner : CommandRunner, optional
        コマンド実行の実装。テストではここを差し替える
        （CI の self-hosted runner には nvidia-smi が無い）。
    """

    timeout_s: float = 10.0
    runner: CommandRunner = field(default=_default_runner)
    resource_kind: str = "gpu"

    def observe(self) -> Observation:
        """GPU が使用中かを実測する。"""
        code, out = self.runner(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            self.timeout_s,
        )
        if code != 0:
            return Observation(busy=None, error=f"nvidia-smi が失敗した (rc={code})")

        pids = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
        detail: dict[str, object] = {"compute_pids": len(pids)}
        detail.update(self._gpu_detail())
        return Observation(busy=bool(pids), detail=detail)

    def _gpu_detail(self) -> dict[str, object]:
        """総使用量と使用率を取得する。取れなければ空の dict を返す。"""
        code, out = self.runner(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            self.timeout_s,
        )
        if code != 0:
            return {}
        line = out.strip().splitlines()[0] if out.strip() else ""
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            return {}

        detail: dict[str, object] = {}
        vram = _as_int(parts[0], _MAX_VRAM_MIB)
        if vram is not None:
            detail["vram_mib"] = vram
        util = _as_int(parts[1], _MAX_UTIL)
        if util is not None:
            detail["util"] = util
        return detail


def _as_int(text: str, upper: int) -> int | None:
    """整数として妥当な範囲なら int にする。範囲外・解釈不能なら None。"""
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    if value < 0 or value > upper:
        return None
    return value
