"""資源の実測プローブ。

宣言（掲示板）より実測を優先するための層。すべてのプローブは
「判定できない」を None で表現し、例外を投げない。判断材料が欠けたときは
呼び出し側が fail-open で通す。
"""

from .base import FakeProbe, Observation, Probe

__all__ = ["FakeProbe", "Observation", "Probe"]
