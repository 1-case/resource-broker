"""プローブの抽象と、テスト用のフェイク実装。

CI は self-hosted の Linux ARM64 で回り、そこに GPU も COM ポートも無い。
また実 GPU を占有するテストを書いてはならない（CLAUDE.md「Testing Constraints」）。
そのため資源の実測は必ずこの抽象を介し、テストでは FakeProbe に差し替える。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Observation:
    """実測の結果。

    Attributes
    ----------
    busy : bool or None
        資源が使用中なら True、空いていれば False、**判定できなければ None**。
        None は「プローブが使えなかった」を意味し、「空いている」ではない。
    detail : dict
        付随情報（VRAM 使用量・使用率など）。掲示板の ``observed`` に載せる。
    error : str or None
        判定できなかった理由。監査ログに残す用途。
    """

    busy: bool | None
    detail: dict[str, object] = field(default_factory=dict)
    error: str | None = None

    @property
    def known(self) -> bool:
        """判定できたかどうか。"""
        return self.busy is not None


@runtime_checkable
class Probe(Protocol):
    """資源の実測を行うもの。

    実装は**例外を投げてはならない**。失敗は ``Observation(busy=None, error=...)``
    で表現する。
    """

    resource_kind: str

    def observe(self) -> Observation:
        """現在の使用状況を実測する。"""
        ...


@dataclass
class FakeProbe:
    """テスト用のプローブ。任意の Observation を返す。"""

    result: Observation
    resource_kind: str = "fake"
    calls: int = 0

    def observe(self) -> Observation:
        """設定された結果をそのまま返し、呼び出し回数を数える。"""
        self.calls += 1
        return self.result


@dataclass
class ExplodingProbe:
    """例外を投げるプローブ。fail-open の守護テストに使う。

    実装側の規約は「例外を投げない」だが、規約を破る実装が混ざっても
    呼び出し側が落ちないことを検証するために用意する。
    """

    resource_kind: str = "exploding"

    def observe(self) -> Observation:
        """常に例外を投げる。"""
        raise RuntimeError("プローブが壊れている")


def observe_safely(probe: Probe | None) -> Observation:
    """プローブを呼び、どんな失敗でも Observation に畳み込む。

    プローブが None（未対応の資源種別）でも、例外を投げる実装でも、
    ここで必ず ``busy=None`` の Observation に変換する。fail-open の要である。

    Parameters
    ----------
    probe : Probe or None
        実行するプローブ。

    Returns
    -------
    Observation
        判定できなければ ``busy=None``。
    """
    if probe is None:
        return Observation(busy=None, error="プローブが登録されていない")
    try:
        result = probe.observe()
    except Exception as exc:  # noqa: BLE001 - fail-open のため全例外を畳み込む
        return Observation(busy=None, error=f"プローブが例外を投げた: {exc}")
    if not isinstance(result, Observation):
        return Observation(busy=None, error="プローブが Observation を返さなかった")
    return result
