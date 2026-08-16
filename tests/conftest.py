"""テスト共通のフィクスチャ。

実運用の掲示板（``%LOCALAPPDATA%\\resource-broker``）を絶対に触らないよう、
すべてのテストは一時ディレクトリ上の Board を使う。
"""

from __future__ import annotations

from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def _repository_stays_clean() -> "Iterator[None]":
    """**テストが作業ツリーを汚していないこと**を毎回確かめる。

    掲示板のパスは差し替えているが、それ以外の書き込み先は誰も見ていなかった。
    実際、相乗りの後始末を検証するテストが ``os.getcwd()``（＝リポジトリのルート）配下に
    ディレクトリを作り、空フォルダが残った。**書いてよいのは tmp_path の中だけ**である。
    """
    root = Path(__file__).resolve().parent.parent
    before = {p.name for p in root.iterdir()}
    yield
    added = {p.name for p in root.iterdir()} - before
    # 実行中に生成される正当なもの（キャッシュ類）は除く。
    added -= {"__pycache__", ".pytest_cache", ".ruff_cache", ".coverage"}
    assert not added, f"テストがリポジトリ直下にファイルを残した: {sorted(added)}"
