"""resource-broker: 並行する Claude Code セッション間で有限資源の使用状況を共有する掲示板。

設計上の最重要原則は fail-open である。本パッケージが壊れたときにユーザーの作業を
止めてはならない。詳細は DESIGN.md「Design Principles」および
CLAUDE.md「Domain-Specific Rules」を参照。
"""

__version__ = "0.3.0"
