"""ラッパー ``rb run`` を検証する。

ラッパーの存在理由はただ 1 つ、**解放を人の意思に頼らないこと**である。
手動運用の検証で、ジョブ完了から解放まで 77 秒のあいだ掲示板が嘘をつく状態が起きた。
したがってここで最も重く守るのは「どう終わってもエントリが残らない」ことである。

実プロセスを起動するテストは ``sys.executable`` の短命なコマンドだけに限る。
資源には一切触れない（CLAUDE.md「Testing Constraints」）。
"""

from __future__ import annotations

import subprocess
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


# --- 子孫まで確実に止める -------------------------------------------------------


class FakeProcess:
    """終了を待つ子プロセスの代役。

    実プロセスを起動せずに「どの順で止めにいったか」を検証する
    （CLAUDE.md「Testing Constraints」）。

    Parameters
    ----------
    survives : int
        何回目の ``wait`` までタイムアウトさせるか。SIGTERM を無視する子を模す。
    """

    def __init__(self, survives: int = 0) -> None:
        self.pid = 4242
        self.waits = 0
        self.survives = survives
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits <= self.survives:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_stop_escalates_when_the_child_ignores_the_first_signal() -> None:
    """SIGTERM を無視する子は強制終了へ昇格する。

    待って諦めると、掲示板から消えた資源を掴んだままのプロセスが残る。
    **掲示板は空・資源は掴まれたまま**という最も検出しにくい不整合になる。
    """
    process = FakeProcess(survives=1)
    forces: list[bool] = []

    def kill_tree(pid: int, force: bool) -> bool:
        forces.append(force)
        return True

    runner._stop(process, kill_tree=kill_tree, timeout_s=0)  # type: ignore[arg-type]

    assert forces == [False, True]  # 穏当に頼んでから、強制する
    assert process.waits == 2


def test_stop_does_not_escalate_when_the_child_exits() -> None:
    """素直に終わった子には強制終了を送らない。"""
    process = FakeProcess()
    forces: list[bool] = []

    def kill_tree(pid: int, force: bool) -> bool:
        forces.append(force)
        return True

    runner._stop(process, kill_tree=kill_tree, timeout_s=0)  # type: ignore[arg-type]

    assert forces == [False]


def test_stop_falls_back_to_single_process_termination() -> None:
    """プロセスツリーごと止められない環境では単体終了へ退避する。

    何もしないよりはよい。直接の子だけでも止まれば、多くの場合は資源が解放される。
    """
    process = FakeProcess(survives=1)

    runner._stop(process, kill_tree=lambda _pid, _force: False, timeout_s=0)

    assert process.terminated is True
    assert process.killed is True


def test_posix_kill_tree_escalates_the_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX ではプロセスグループへ SIGTERM → SIGKILL の順で送る。

    Windows の ``signal`` に ``SIGKILL`` が無いため、シグナル番号は import 時に
    解決してある（この検証を Windows でも回せることが、その必要性の裏づけである）。
    """
    import os

    sent: list[int] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(sig), raising=False)

    assert runner._kill_tree_posix(4242, False) is True
    assert runner._kill_tree_posix(4242, True) is True
    assert sent == [runner._SIGTERM, runner._SIGKILL]


def test_posix_kill_tree_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """シグナルを送れなければ False。呼び出し側が単体終了へ退避する。"""
    import os

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ProcessLookupError("もう居ない")

    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(os, "killpg", explode, raising=False)

    assert runner._kill_tree_posix(4242, False) is False


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
