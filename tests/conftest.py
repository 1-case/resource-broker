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
def _pin_language_to_japanese(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定では言語判定を ``ja`` に固定する。**テストをロケールから独立させるため。**

    フックの言語判定は環境変数 → Claude Code の設定 → OS のロケール → 既定 ``ja``
    の順で決まる（``docs/DESIGN.md``「Hook Spec」）。テストが ``RESOURCE_BROKER_LANG``
    を明示しないと、**実行するマシンの OS ロケール次第で結果が変わる**——実際に
    macOS / Windows の CI ランナー（英語ロケール）でだけ、日本語の文言を照合していた
    アサーションが落ちた（issue #26）。ubuntu が偶然 ``C``/``POSIX`` ロケールで
    通っていただけで、判定自体は正しく動いていた。

    ここで ``ja`` に固定するのは「日本語が正本」という方針に沿うが、**これで
    英語側を誰も見ない状態にしない**——``RESOURCE_BROKER_LANG=en`` を明示する
    専用のテスト（``tests/test_hook_language_switch.py``）で英語の出力を別途守る。
    このフィクスチャは ``monkeypatch`` を使うので、個々のテストが同じキーを
    上書きすれば（後勝ちで）そちらが有効になる。
    """
    monkeypatch.setenv("RESOURCE_BROKER_LANG", "ja")


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
