"""テスト共通のフィクスチャ。

実運用の掲示板（``%LOCALAPPDATA%\\resource-broker``）を絶対に触らないよう、
すべてのテストは一時ディレクトリ上の Board を使う。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resource_broker.board import Board


@pytest.fixture
def board(tmp_path: Path) -> Board:
    """一時ディレクトリ上の掲示板。"""
    return Board(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_board_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """既定の掲示板ルートも一時ディレクトリへ向ける。

    ``Board()`` を引数なしで作るコードパスがテストに紛れ込んでも、
    実運用の掲示板を汚さないようにするための保険である。
    """
    monkeypatch.setenv("RESOURCE_BROKER_HOME", str(tmp_path / "default-home"))
