"""nvidia-smi プローブを、コマンド実行を差し替えて検証する。

CI の self-hosted runner（Linux ARM64）には nvidia-smi が無く、
実 GPU を占有するテストも禁止されている（CLAUDE.md「Testing Constraints」）。
そのため runner を注入して実際の出力文字列だけを再現する。
"""

from __future__ import annotations

from resource_broker.probes.gpu import NvidiaSmiProbe

# 実機（RTX 4060 Laptop / Windows WDDM）で観測した実際の出力。
# used_gpu_memory が [N/A] になるのは WDDM の制約である。
REAL_COMPUTE_APPS = "24284\n"
REAL_GPU_LINE = "2717, 100\n"


def probe_with(responses: dict[str, tuple[int, str]]) -> NvidiaSmiProbe:
    """クエリ種別ごとに固定応答を返すプローブを作る。"""

    def runner(args: list[str], _timeout: float) -> tuple[int, str]:
        key = "compute" if any("compute-apps" in a for a in args) else "gpu"
        return responses.get(key, (1, ""))

    return NvidiaSmiProbe(runner=runner)


def test_reports_busy_when_compute_apps_exist() -> None:
    """compute プロセスがいれば使用中と判定する。"""
    probe = probe_with({"compute": (0, REAL_COMPUTE_APPS), "gpu": (0, REAL_GPU_LINE)})
    result = probe.observe()

    assert result.busy is True
    assert result.detail["compute_pids"] == 1
    assert result.detail["vram_mib"] == 2717
    assert result.detail["util"] == 100


def test_reports_idle_when_no_compute_apps() -> None:
    """compute プロセスがいなければ空きと判定する。"""
    probe = probe_with({"compute": (0, "\n"), "gpu": (0, "0, 0\n")})
    result = probe.observe()

    assert result.busy is False
    assert result.detail["compute_pids"] == 0


def test_missing_nvidia_smi_is_unknown_not_idle() -> None:
    """nvidia-smi が無い環境では「空き」ではなく「判定不能」を返す。"""
    probe = probe_with({"compute": (-1, "")})

    assert probe.observe().busy is None


def test_timeout_is_unknown_not_idle() -> None:
    """タイムアウトも判定不能として扱う。

    dGPU が低電力状態にあると初回応答が遅い（実測で約 2 秒）。
    タイムアウトを「空き」と誤読すると衝突を招く。
    """
    probe = probe_with({"compute": (-2, "")})

    assert probe.observe().busy is None


def test_ignores_non_numeric_lines_in_compute_apps() -> None:
    """ヘッダや [N/A] のような非数値行を PID として数えない。"""
    probe = probe_with({"compute": (0, "pid\n[N/A]\n24284\n"), "gpu": (1, "")})

    assert probe.observe().detail["compute_pids"] == 1


def test_drops_out_of_range_sensor_values() -> None:
    """範囲外のセンサ値は detail に載せない。

    アイドル時に power.draw が 590.01 W と報告された実績がある。
    実測値であっても sanity check を通す。
    """
    probe = probe_with({"compute": (0, "\n"), "gpu": (0, "-5, 4000\n")})
    detail = probe.observe().detail

    assert "vram_mib" not in detail
    assert "util" not in detail


def test_survives_unparsable_gpu_detail() -> None:
    """detail が読めなくても busy の判定は成立する。"""
    probe = probe_with({"compute": (0, REAL_COMPUTE_APPS), "gpu": (0, "壊れた出力\n")})
    result = probe.observe()

    assert result.busy is True
    assert result.detail["compute_pids"] == 1
