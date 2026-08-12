"""ラッパー ``rb run`` を検証する。

ラッパーの存在理由はただ 1 つ、**解放を人の意思に頼らないこと**である。
手動運用の検証で、ジョブ完了から解放まで 77 秒のあいだ掲示板が嘘をつく状態が起きた。
したがってここで最も重く守るのは「どう終わってもエントリが残らない」ことである。

実プロセスを起動するテストは ``sys.executable`` の短命なコマンドだけに限る。
資源には一切触れない（CLAUDE.md「Testing Constraints」）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from resource_broker import runner
from resource_broker.board import Board
from resource_broker.cli import main


def run(tmp_path: Path, *args: str) -> int:
    """一時的な掲示板に対して CLI を実行する。"""
    return main(["--home", str(tmp_path), *args])


def rb_run(tmp_path: Path, *command: str, res: str = "GPU0", **flags: str) -> int:
    """``rb run`` を必須項目つきで実行する。"""
    argv = [
        "run",
        "--res",
        res,
        "--job",
        flags.get("job", "検証ジョブ"),
        "--observed",
        flags.get("observed", "調べた"),
        "--eta",
        flags.get("eta", "10m"),
        "--found",
        flags.get("found", "free"),
    ]
    if "log" in flags:
        argv += ["--log", flags["log"]]
    return run(tmp_path, *argv, "--", *command)


def entries(tmp_path: Path) -> list[str]:
    """掲示板に残っているエントリの資源 ID。"""
    return [entry.resource for entry in Board(tmp_path).list_all()]


# --- 終了経路ごとに「エントリが残らない」ことを守る -------------------------------


def test_entry_is_released_after_success(tmp_path: Path) -> None:
    """正常終了したらエントリは残らない。"""
    assert rb_run(tmp_path, sys.executable, "-c", "print('ok')") == 0

    assert entries(tmp_path) == []


def test_child_exit_code_is_propagated(tmp_path: Path) -> None:
    """子プロセスの終了コードをそのまま返す。

    ラッパーが握りつぶすと、失敗したジョブが成功したように見える。
    """
    assert rb_run(tmp_path, sys.executable, "-c", "raise SystemExit(3)") == 3

    assert entries(tmp_path) == []


def test_entry_is_released_after_child_crash(tmp_path: Path) -> None:
    """子プロセスが例外で落ちてもエントリは残らない。"""
    code = rb_run(tmp_path, sys.executable, "-c", "raise RuntimeError('落ちた')")

    assert code != 0
    assert entries(tmp_path) == []


def test_entry_is_released_when_command_is_missing(tmp_path: Path) -> None:
    """起動すらできなくてもエントリは残らない。"""
    code = rb_run(tmp_path, "この名前のコマンドは存在しない-rb-test")

    assert code == runner.EXIT_COMMAND_NOT_FOUND
    assert entries(tmp_path) == []


def test_failure_before_spawn_does_not_report_success(tmp_path: Path) -> None:
    """起動より**手前**で壊れても 0 を返さない。

    `main` の catch-all は全ての内部例外を握って 0 を返す。`run` でそれをやると、
    コマンドを 1 度も起動していないのに呼び出し側が成功と読む。
    fail-open は「資源アクセスを止めない」原則であって、「走らなかったジョブを
    成功と報告してよい」ではない。

    再現は資源 ID を空にする経路（`naming.normalize` が例外を投げる）。
    SPAWN を壊すだけのテストでは、この手前の経路を守護できない。
    """
    code = main(
        [
            "--home",
            str(tmp_path),
            "run",
            "--res",
            "",
            "--job",
            "x",
            "--observed",
            "調べた",
            "--eta",
            "10m",
            "--",
            sys.executable,
            "-c",
            "print(1)",
        ]
    )

    assert code == runner.EXIT_CANNOT_EXECUTE
    assert entries(tmp_path) == []


def test_broken_wrapper_does_not_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動処理が壊れても、エントリは残らず、かつ 0 を返さない。

    fail-open は「資源アクセスを止めない」原則であって、「走らなかったジョブを
    成功と報告してよい」ではない。ここで 0 を返すと、ラッパーの故障が
    ジョブの成功に化ける。
    """

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("起動の内部で壊れた")

    monkeypatch.setattr("resource_broker.cli.SPAWN", explode)
    code = rb_run(tmp_path, "なんでもよい")

    assert code == runner.EXIT_CANNOT_EXECUTE
    assert entries(tmp_path) == []


def test_entry_is_released_on_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C で中断されてもエントリは残らない。"""

    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("resource_broker.cli.SPAWN", interrupt)

    assert rb_run(tmp_path, "なんでもよい") == 130
    assert entries(tmp_path) == []


# --- 宣言の中身 -----------------------------------------------------------------


def test_run_records_its_own_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ラッパーは自分の PID を記録する。

    手動 ``claim`` が PID を記録しないのと対になる規則である。ラッパーは
    ジョブと同じ寿命を持つため、ここでの生存確認だけが意味を持つ。
    """
    captured: dict[str, object] = {}

    def capture(argv: list[str], log_path: Path, env: dict[str, str]) -> int:
        board = Board(tmp_path)
        entry = next(iter(board.list_all()))
        captured["pid"] = entry.pid
        captured["log"] = entry.log
        return 0

    monkeypatch.setattr("resource_broker.cli.SPAWN", capture)
    rb_run(tmp_path, "なんでもよい")

    assert captured["pid"] is not None
    assert captured["log"]  # ログのパスが宣言に載っている


def test_run_is_blocked_by_a_live_declaration(tmp_path: Path) -> None:
    """他者が宣言中なら、コマンドを実行せずに 1 を返す。"""
    run(tmp_path, "claim", "GPU0", "--job", "先客", "--observed", "調べた", "--eta", "1h")
    marker = tmp_path / "実行された.txt"

    code = rb_run(tmp_path, sys.executable, "-c", f"open(r'{marker}', 'w').close()")

    assert code == 1
    assert not marker.exists()  # 実行していない
    assert entries(tmp_path)  # 先客の宣言は消えていない


def test_run_without_command_does_not_claim(tmp_path: Path) -> None:
    """`--` の後ろが空なら、宣言もしない（引数の不備として 2 を返す）。"""
    argv = ["run", "--res", "GPU0", "--job", "x", "--observed", "調べた", "--eta", "5m"]

    assert run(tmp_path, *argv) == 2
    assert entries(tmp_path) == []


# --- ログの強制 -----------------------------------------------------------------


def test_output_is_written_to_the_log(tmp_path: Path) -> None:
    """子プロセスの stdout / stderr がログに落ちる。"""
    log = tmp_path / "job.log"
    rb_run(
        tmp_path,
        sys.executable,
        "-c",
        "import sys; print('標準出力'); print('標準エラー', file=sys.stderr)",
        log=str(log),
    )

    text = log.read_text(encoding="utf-8", errors="replace")
    assert "標準出力" in text
    assert "標準エラー" in text
    assert "rb run:" in text  # 何を実行したかの記録


def test_default_log_path_is_under_the_board_root(tmp_path: Path) -> None:
    """--log を省略すると掲示板ルート配下に出る。

    他セッションが読めることが要件なので、プロジェクト配下には置かない。
    """
    path = runner.build_log_path(tmp_path, "host::GPU0")

    assert path.parent == tmp_path / runner.LOG_DIR
    assert path.suffix == ".log"


def test_child_environment_disables_buffering() -> None:
    """子プロセスの出力バッファリングを無効にする。

    バッファに溜まったまま出てこないログは、観測点として役に立たない。
    """
    assert runner.child_environment({})["PYTHONUNBUFFERED"] == "1"


def test_command_line_is_not_interpreted(tmp_path: Path) -> None:
    """コマンド行を解釈しない。

    `python` を探して `-u` を挿す実装にすると `uv run python` を取りこぼす。
    バッファリングの無効化は環境変数で行い、コマンドには触れない。
    """
    captured: dict[str, object] = {}

    def capture(argv: list[str], log_path: Path, env: dict[str, str]) -> int:
        captured["argv"] = argv
        return 0

    original = "python train.py --epochs 10 -u-ではない".split()
    import resource_broker.cli as cli_module

    cli_module.SPAWN = capture  # type: ignore[assignment]
    try:
        rb_run(tmp_path, *original)
    finally:
        cli_module.SPAWN = runner.default_spawn  # type: ignore[assignment]

    assert captured["argv"] == original
