"""幽霊判定の決定表を検証する。

実測の扱いが**非対称**であることが要点である。
「使用中」は単独で確定するが、「空き」は宣言を退ける根拠にならない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from resource_broker import liveness
from resource_broker.liveness import Verdict
from resource_broker.probes.base import Observation

JST = timezone(timedelta(hours=9))
BOOT = datetime(2026, 8, 12, 4, 27, 53, tzinfo=JST)
NOW = BOOT + timedelta(hours=2)

BEFORE_BOOT = BOOT - timedelta(hours=1)
FRESH = NOW - timedelta(minutes=1)  # 猶予の内側（宣言したばかり）
AGED = NOW - timedelta(hours=1)  # 猶予の外側

UNKNOWN = Observation(busy=None, error="プローブが使えない")
BUSY = Observation(busy=True)
IDLE = Observation(busy=False)


def judge(**overrides: object) -> Verdict:
    """既定の材料に上書きをかけて判定する。"""
    materials: dict[str, object] = {
        "has_entry": True,
        "since": AGED,
        "boot": BOOT,
        "observation": UNKNOWN,
        "pid_alive": True,
        "now": NOW,
    }
    materials.update(overrides)
    return liveness.judge(**materials)  # type: ignore[arg-type]


def test_no_entry_is_free() -> None:
    """エントリが無ければ空き。"""
    assert judge(has_entry=False) == Verdict.FREE


def test_probe_busy_is_decisive() -> None:
    """実測が使用中なら、宣言が再起動前でも使用中と判定する。

    宣言が幽霊でも、実際に誰かが使っているなら使えないことに変わりはない。
    """
    assert judge(since=BEFORE_BOOT, observation=BUSY) == Verdict.HELD


def test_declaration_before_boot_is_definitively_stale() -> None:
    """再起動をまたいだ宣言は確定的な幽霊。全 PID が無効になっている。"""
    assert judge(since=BEFORE_BOOT) == Verdict.STALE_REBOOT


def test_fresh_declaration_survives_an_idle_probe() -> None:
    """宣言直後は、実測が空きでも宣言を信じる。

    これが無いと、``claim`` してからジョブが資源を掴むまでの間
    （モデルのロードに数十秒から数分かかる）に他セッションへ奪われる。
    防ごうとしている衝突そのものになる。
    """
    assert judge(since=FRESH, observation=IDLE, pid_alive=False) == Verdict.HELD


def test_idle_probe_alone_does_not_free_the_resource() -> None:
    """猶予を過ぎても、実測が空きなだけでは幽霊にしない。

    長時間ジョブでも、資源を使わない工程では空きに見える
    （E008 スイープの compare ステップは GPU を使わない）。
    """
    verdict = judge(observation=IDLE, pid_alive=True)
    assert verdict == Verdict.HELD


def test_manual_claim_is_never_evicted_by_an_idle_probe() -> None:
    """PID が記録されていない宣言（手動 claim）は実測だけでは退けられない。

    手動 ``claim`` では PID を記録しない。生存確認ができないため、
    退けてよいのは再起動をまたいだ場合と明示的な解放だけになる。
    """
    verdict = judge(observation=IDLE, pid_alive=None)
    assert verdict == Verdict.HELD
    assert not liveness.is_free(verdict)


def test_aged_idle_and_dead_pid_is_stale() -> None:
    """猶予経過・実測が空き・宣言プロセスが死亡、の三点がそろえば幽霊。"""
    verdict = judge(observation=IDLE, pid_alive=False)
    assert verdict == Verdict.STALE_PROBE
    assert liveness.is_free(verdict)


def test_dead_pid_without_probe_confirmation_is_uncertain() -> None:
    """PID が死んでいても、実測で裏が取れなければ空きにしない。

    長時間ジョブはプロセスを入れ替えながら継続することがある。
    """
    verdict = judge(pid_alive=False)
    assert verdict == Verdict.UNCERTAIN
    assert not liveness.is_free(verdict)


def test_unparsable_since_is_uncertain_not_free() -> None:
    """宣言時刻が壊れていても、勝手に空きとはみなさない。"""
    verdict = judge(since=None)
    assert verdict == Verdict.UNCERTAIN
    assert not liveness.is_free(verdict)


def test_missing_boot_time_falls_back_to_declaration() -> None:
    """起動時刻が取得できなくても判定は成立し、宣言が生きているとみなす。"""
    assert judge(boot=None) == Verdict.HELD


def test_grace_boundary_is_exclusive() -> None:
    """猶予の境界ちょうどでは、猶予は切れている。"""
    since = NOW - liveness.DEFAULT_GRACE
    assert judge(since=since, observation=IDLE, pid_alive=False) == Verdict.STALE_PROBE


def test_grace_is_configurable() -> None:
    """猶予は呼び出し側で調整できる。"""
    verdict = judge(
        since=AGED,
        observation=IDLE,
        pid_alive=False,
        grace=timedelta(hours=2),
    )
    assert verdict == Verdict.HELD


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_has_an_explanation(verdict: Verdict) -> None:
    """すべての判定に日本語の説明がある（CLI とフックの説明文で使う）。"""
    assert liveness.explain(verdict)


@pytest.mark.parametrize("verdict", list(Verdict))
def test_is_free_matches_the_declared_set(verdict: Verdict) -> None:
    """is_free と FREE_VERDICTS が食い違わない。"""
    assert liveness.is_free(verdict) == (verdict in liveness.FREE_VERDICTS)


def test_uncertain_is_not_treated_as_free() -> None:
    """裏が取れないときは fail-safe に倒す（掲示板は正常に読めているため）。"""
    assert Verdict.UNCERTAIN not in liveness.FREE_VERDICTS
