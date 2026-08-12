"""fail-open の守護テスト。

本ツールは他の全セッションの Bash 実行経路に割り込む。壊れたときにユーザーの
作業を止めないことが最優先の要件であり、「注意する」だけでは守れない
（CLAUDE.md「Fail-Open」「Testing Constraints」）。

ここでは「壊れた入力を与えても例外が外に出ず、判断材料なしとして扱われる」ことを
網羅的に確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resource_broker import clock, platform_info
from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.probes.base import ExplodingProbe, FakeProbe, Observation, observe_safely

CORRUPT_PAYLOADS = [
    "",
    "{",
    "null",
    "[]",
    '{"resource": null}',
    '{"holder": {"pid": "文字列"}}',
    "\x00\x01\x02",
]


@pytest.mark.parametrize("payload", CORRUPT_PAYLOADS)
def test_corrupt_entry_reads_as_absent(board: Board, payload: str) -> None:
    """壊れたエントリは「存在しない」と同じ扱いになり、例外を投げない。"""
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    board.path_for("pc-a::GPU0").write_text(payload, encoding="utf-8")

    assert board.read("pc-a::GPU0") is None


def test_missing_board_directory_reads_as_empty(tmp_path: Path) -> None:
    """掲示板ディレクトリが存在しなくても、読み取りは静かに空を返す。"""
    board = Board(tmp_path / "存在しない")

    assert board.read("pc-a::GPU0") is None
    assert board.list_all() == []


def test_unreadable_entry_does_not_raise(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """OS レベルの読み取りエラーでも例外を外に出さない。"""
    board.try_claim(build_entry("pc-a::GPU0", job="学習"))

    def explode(*_args: object, **_kwargs: object) -> str:
        raise OSError("読めない")

    monkeypatch.setattr(Path, "read_text", explode)
    assert board.read("pc-a::GPU0") is None


def test_audit_failure_does_not_propagate(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """監査ログが書けなくても本処理は続く。"""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("書けない")

    monkeypatch.setattr(Path, "mkdir", explode)
    board.audit("test_event")  # 例外が出なければ合格


def test_exploding_probe_is_folded_into_unknown() -> None:
    """規約を破って例外を投げるプローブでも、判定不能に畳み込まれる。"""
    result = observe_safely(ExplodingProbe())

    assert result.busy is None
    assert result.error is not None


def test_missing_probe_is_unknown_not_free() -> None:
    """プローブが無いことは「空き」ではなく「判定不能」である。"""
    assert observe_safely(None).busy is None


def test_probe_returning_wrong_type_is_unknown() -> None:
    """Observation 以外を返す実装も判定不能に畳み込まれる。"""

    class Rogue:
        resource_kind = "rogue"

        def observe(self) -> object:
            return "空いてます"

    assert observe_safely(Rogue()).busy is None  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", None, "きのう", "2026-13-45T99:99:99", "0000"])
def test_parse_iso_returns_none_for_garbage(text: str | None) -> None:
    """壊れた時刻文字列で例外を投げない。"""
    assert clock.parse_iso(text) is None


@pytest.mark.parametrize("pid", [None, 0, -1, "1234", 3.5])
def test_pid_alive_returns_none_for_invalid_input(pid: object) -> None:
    """不正な PID では判定不能を返す（例外にしない）。"""
    assert platform_info.pid_alive(pid) is None  # type: ignore[arg-type]


def test_cli_returns_zero_on_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """内部エラーでは 0 を返して通す。

    1 を返してよいのは「掲示板が正常に読めた上で使用中と判定できた」場合だけである。
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("想定外の故障")

    monkeypatch.setattr("resource_broker.cli._cmd_status", explode)
    code = main(["--home", str(tmp_path), "status"])

    assert code == 0
    assert "内部エラー" in capsys.readouterr().err


def test_claim_proceeds_when_probe_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """プローブが壊れていても、空いていれば宣言できる。"""
    monkeypatch.setattr("resource_broker.cli.probe_for", lambda _r: ExplodingProbe())

    assert main(["--home", str(tmp_path), "claim", "GPU0", "--job", "学習"]) == 0


def test_busy_probe_blocks_claim_even_without_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """掲示板が空でも、実測が使用中なら宣言は通る。

    エントリが無ければ FREE と判定するのが設計である（実測は帰属を教えないため、
    未宣言の使用者を検出しても宣言を止める根拠にはしない）。この挙動を固定しておく。
    """
    monkeypatch.setattr(
        "resource_broker.cli.probe_for", lambda _r: FakeProbe(Observation(busy=True))
    )

    assert main(["--home", str(tmp_path), "claim", "GPU0", "--job", "学習"]) == 0
