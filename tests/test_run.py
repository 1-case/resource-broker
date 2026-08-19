"""ラッパー ``rb run`` を検証する。

ラッパーの存在理由はただ 1 つ、**解放を人の意思に頼らないこと**である。
手動運用の検証で、ジョブ完了から解放まで 77 秒のあいだ掲示板が嘘をつく状態が起きた。
したがってここで最も重く守るのは「どう終わってもエントリが残らない」ことである。

実プロセスを起動するテストは ``sys.executable`` の短命なコマンドだけに限る。
資源には一切触れない（CLAUDE.md「Testing Constraints」）。
"""

from __future__ import annotations

import builtins
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from resource_broker import runner
from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize


def _path_of(board: Board, resource_id: str) -> Path:
    """その資源の宣言が置かれているパス（1 件だけある前提のテスト用）。

    平坦化でファイル名は nonce になったので、**名前から場所を組み立てられない**。
    中身で引く。
    """
    found = board.pairs_for(resource_id)
    assert len(found) == 1, found
    return found[0][0]


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


def test_run_does_not_claim_success_when_the_board_could_not_record_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """掲示板に残せていないのに「宣言しました」と言わない。

    ``acquire`` は掲示板が書けない・壊れたファイルが居座る場合でも ``entry`` を返し、
    ``declared=False`` で通す（fail-open）。そこを分岐しないと、**他セッションから
    見えない利用を成功した宣言として偽装する**ことになる。掲示板の唯一の役目は
    「他セッションから見えること」であり、そこが果たせていないなら成功と言えない。

    **実行は止めない。** fail-open は「資源アクセスを止めない」原則である。
    """
    from resource_broker.naming import normalize

    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    # 読めないエントリが居座っている＝ O_EXCL で作れず、読んでも None になる状態
    _path_of(board, normalize("GPU0")).write_text("{ これは JSON ではない", encoding="utf-8")
    marker = tmp_path / "実行された.txt"
    capsys.readouterr()

    code = rb_run(tmp_path, sys.executable, "-c", f"open(r'{marker}', 'w').close()")
    captured = capsys.readouterr()

    assert code == 0  # 実行は止めない
    assert marker.exists()  # 実際に走っている
    assert "宣言しました" not in captured.out
    assert "掲示板に残せていません" in captured.err
    assert "他セッションからは見えません" in captured.err


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


# --- ログの容量と保持期限 -------------------------------------------------------


def test_log_is_capped_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上限に達したらログの追記をやめ、**やめたことを 1 行残す**。

    ログは掲示板・監査ログと同じルートに置く。出力の多い長時間ジョブ 1 つでディスクが
    埋まると、掲示板も監査ログも書けなくなり、**全セッションが掲示板を失ったまま
    fail-open で資源を取り合う**。

    黙って捨ててはならない。読む側が「ここでジョブが止まった」と読む
    （CLAUDE.md「Silence Is Not Success」）。
    """
    monkeypatch.setattr(runner, "MAX_LOG_BYTES", 2000)
    log = tmp_path / "job.log"

    code = rb_run(
        tmp_path,
        sys.executable,
        "-c",
        "print('x' * 200000)",
        log=str(log),
    )

    text = log.read_text(encoding="utf-8", errors="replace")
    assert code == 0  # ジョブは止めない
    assert "上限" in text and "破棄した" in text
    assert log.stat().st_size < 20000  # 20 万バイトは落ちていない


def test_the_job_keeps_running_after_the_log_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限に達しても子は最後まで走る。

    上限を「読むのをやめる」で実装すると、パイプが詰まって子が書き込みでブロックする。
    **ログの上限がジョブの停止に化ける**のが最悪の結末である。
    """
    monkeypatch.setattr(runner, "MAX_LOG_BYTES", 100)
    marker = tmp_path / "最後まで走った.txt"

    code = rb_run(
        tmp_path,
        sys.executable,
        "-c",
        f"print('y' * 500000); open(r'{marker}', 'w').close()",
        log=str(tmp_path / "job.log"),
    )

    assert code == 0
    assert marker.exists(), "上限に達したところで子が止まっている"


def test_old_logs_are_pruned(tmp_path: Path) -> None:
    """保持期限を過ぎたログは掃除する。世代管理が無いと溜まり続ける。"""
    logs = tmp_path / runner.LOG_DIR
    logs.mkdir(parents=True, exist_ok=True)
    old = logs / "古い.log"
    fresh = logs / "新しい.log"
    old.write_text("古い", encoding="utf-8")
    fresh.write_text("新しい", encoding="utf-8")
    stale = time.time() - (runner.LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old, (stale, stale))

    removed = runner.prune_old_logs(logs)

    assert removed == 1
    assert old.exists() is False
    assert fresh.exists() is True


def test_pruning_failures_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """掃除に失敗してもジョブを止めない。

    掃除は付随的な仕事である。ここで例外を出すと「掃除できないからジョブが走らない」
    という本末転倒が起きる。
    """
    logs = tmp_path / runner.LOG_DIR
    logs.mkdir(parents=True, exist_ok=True)
    old = logs / "古い.log"
    old.write_text("古い", encoding="utf-8")
    stale = time.time() - (runner.LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old, (stale, stale))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("消せない")

    monkeypatch.setattr(Path, "unlink", refuse)

    assert runner.prune_old_logs(logs) == 0  # 例外を出さない
    assert rb_run(tmp_path, sys.executable, "-c", "print('ok')") == 0


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


# ``group_alive`` は各テストで必ず注入する。既定の :func:`runner._group_alive` は
# 実プロセスグループへ問い合わせるため、偽の PID を渡すと**このマシン上の無関係な
# プロセスグループ**に当たりうる。検証したいのは昇格の順序だけである。


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

    runner._stop(
        process,  # type: ignore[arg-type]
        kill_tree=kill_tree,
        group_alive=lambda _pid: False,
        timeout_s=0,
    )

    assert forces == [False, True]  # 穏当に頼んでから、強制する
    assert process.waits == 2


def test_stop_escalates_when_only_the_grandchild_survives() -> None:
    """**直接の子が素直に終わっても、孫が残っていれば昇格する。**

    推奨している形が ``rb run ... -- uv run python train.py`` であり、直接の子は
    ``uv``、資源を掴むのは孫の ``python`` である。``uv`` が ``SIGTERM`` に応じると
    ``process.wait()`` は成功して戻るため、そこで return すると**孫だけが資源を
    掴んだまま残り、``finally`` が掲示板から宣言を消す**。掲示板は空・資源は
    掴まれたままという、最も検出しにくい不整合になる。
    """
    process = FakeProcess()  # 直接の子は第 1 段で素直に終わる
    forces: list[bool] = []
    group = {"alive": True}  # だが孫が残っている

    def kill_tree(pid: int, force: bool) -> bool:
        forces.append(force)
        if force:
            group["alive"] = False  # 強制終了でグループごと消える
        return True

    runner._stop(
        process,  # type: ignore[arg-type]
        kill_tree=kill_tree,
        group_alive=lambda _pid: group["alive"],
        timeout_s=0,
    )

    assert forces == [False, True]


def test_stop_does_not_escalate_when_the_child_exits() -> None:
    """素直に終わった子には強制終了を送らない。"""
    process = FakeProcess()
    forces: list[bool] = []

    def kill_tree(pid: int, force: bool) -> bool:
        forces.append(force)
        return True

    runner._stop(
        process,  # type: ignore[arg-type]
        kill_tree=kill_tree,
        group_alive=lambda _pid: False,
        timeout_s=0,
    )

    assert forces == [False]


def test_stop_does_not_escalate_when_group_liveness_is_unknown() -> None:
    """生存を判定できない環境では昇格しない。

    Windows にはプロセスグループの概念が無く、``taskkill /T`` が木ごと落とすため
    直接の子の終了で足りる。ここで「判定できない」を「生きている」に倒すと、
    **素直に終わろうとしている子を毎回強制終了する**ことになる。
    """
    process = FakeProcess()
    forces: list[bool] = []

    def kill_tree(pid: int, force: bool) -> bool:
        forces.append(force)
        return True

    runner._stop(
        process,  # type: ignore[arg-type]
        kill_tree=kill_tree,
        group_alive=lambda _pid: None,
        timeout_s=0,
    )

    assert forces == [False]


def test_stop_falls_back_to_single_process_termination() -> None:
    """プロセスツリーごと止められない環境では単体終了へ退避する。

    何もしないよりはよい。直接の子だけでも止まれば、多くの場合は資源が解放される。
    """
    process = FakeProcess(survives=1)

    runner._stop(
        process,  # type: ignore[arg-type]
        kill_tree=lambda _pid, _force: False,
        group_alive=lambda _pid: False,
        timeout_s=0,
    )

    assert process.terminated is True
    assert process.killed is True


def test_group_liveness_does_not_go_through_getpgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """グループの生存確認で ``getpgid`` を経由しない。

    直接の子を ``wait()`` で回収した後は ``getpgid`` が失敗する。経由すると
    「孫だけが生き残っている」という**まさに見たい状態**が「グループは無い」に化ける。
    """
    import os

    if sys.platform == "win32":
        pytest.skip("Windows にはプロセスグループの概念が無い")

    def explode(_pid: int) -> int:
        raise ProcessLookupError("直接の子は既に回収されている")

    monkeypatch.setattr(os, "getpgid", explode, raising=False)
    monkeypatch.setattr(os, "killpg", lambda _pgid, _sig: None, raising=False)

    assert runner._group_alive(4242) is True


def test_posix_kill_tree_reaches_the_group_after_the_child_was_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接の子を回収した後でも、グループへ ``SIGKILL`` が届く。

    ``getpgid`` が失敗したところで諦めると、資源を掴んでいる孫が残る。
    子は ``start_new_session=True`` で起動しているので ``pgid == 子の PID`` である。
    """
    import os

    def explode(_pid: int) -> int:
        raise ProcessLookupError("直接の子は既に回収されている")

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", explode, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)

    assert runner._kill_tree_posix(4242, True) is True
    assert sent == [(4242, runner._SIGKILL)]


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


def test_a_custom_log_directory_is_never_pruned(tmp_path: Path) -> None:
    """``--log`` で指定した場所の無関係なログを消さない（回帰テスト）。

    掃除を「書き込み先のディレクトリ」に対して行っていたため、
    ``rb run --log runs/e059/train.log`` とすると **``runs/e059/`` にある 7 日より古い
    他の `*.log`（過去の実験ログ等）が黙って消えていた**。掲示板を守るための掃除が、
    本ツールの管理外のファイルへ波及していた。
    """
    mine = tmp_path / "runs" / "e059"
    mine.mkdir(parents=True)
    bystander = mine / "過去の実験.log"
    bystander.write_text("消えてはいけない", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(bystander, (old, old))

    rb_run(tmp_path, sys.executable, "-c", "print('ok')", log=str(mine / "job.log"))

    assert bystander.exists(), "--log の指定先にある無関係なログが消えた"


def test_our_own_log_directory_is_pruned(tmp_path: Path) -> None:
    """本ツールのログ置き場は掃除される（掃除そのものは効いている）。"""
    logs = tmp_path / runner.LOG_DIR
    logs.mkdir(parents=True)
    stale = logs / "古い.log"
    stale.write_text("古い", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    rb_run(tmp_path, sys.executable, "-c", "print('ok')")

    assert not stale.exists(), "自分のログ置き場が掃除されていない"


# --- 解放の記録から、走らずに死んだジョブと完走したジョブを区別できること -----------


def audit_reasons(tmp_path: Path) -> list[str]:
    """監査ログに残った ``removed`` の理由。"""
    reasons = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") == "removed":
                reasons.append(str(record.get("reason", "")))
    return reasons


def test_the_release_record_carries_the_exit_code(tmp_path: Path) -> None:
    """解放の理由に子の終了コードが残る。

    残さないと、走らずに即死したジョブと完走したジョブが監査ログ上で同じ 1 行になる。
    実運用で「ETA 2h30m の宣言が 6 秒で解放される」事象を後から追ったとき、
    失敗だったのか短く済んだのかを区別できなかった（CLAUDE.md「Silence Is Not Success」）。
    """
    assert rb_run(tmp_path, sys.executable, "-c", "raise SystemExit(3)") == 3

    assert audit_reasons(tmp_path) == ["rb run の終了（exit=3）"]


def test_a_successful_run_is_distinguishable_from_a_failed_one(tmp_path: Path) -> None:
    """成功と失敗が別の記録になる。**同じ文字列になってはならない。**"""
    assert rb_run(tmp_path, sys.executable, "-c", "print('ok')") == 0
    assert rb_run(tmp_path, sys.executable, "-c", "raise SystemExit(9)") == 9

    ok, failed = audit_reasons(tmp_path)
    assert ok != failed
    assert "exit=0" in ok
    assert "exit=9" in failed


def test_a_command_that_never_started_is_not_recorded_as_success(tmp_path: Path) -> None:
    """起動できなかったジョブが exit=0 として残らない。

    走らなかったジョブを成功と報告しないという規約が、監査ログの側でも成り立つこと。
    """
    assert rb_run(tmp_path, "この実行ファイルは存在しない") != 0

    assert audit_reasons(tmp_path) != ["rb run の終了（exit=0）"]


# --- 相乗りでラッパーを使う（rb run --join） ---------------------------------------


def hold(tmp_path: Path, *, sharing: str = "可") -> None:
    """他セッションの主宣言を 1 件置く（cwd を自分の外にして所有を切る）。"""
    board = Board(tmp_path)
    entry = build_entry(
        normalize("GPU0"),
        job="E061 スモーク",
        session="folnet",
        cwd=str(tmp_path / "other-session"),
        sharing=sharing,
    )
    assert board.declare(entry)


def rb_share_run(tmp_path: Path, *command: str, **flags: str) -> int:
    """``rb run --join`` を必須項目つきで実行する。"""
    return run(
        tmp_path,
        "run",
        "--res",
        "GPU0",
        "--share",
        "--job",
        flags.get("job", "相乗りジョブ"),
        "--observed",
        flags.get("observed", "保持者の相乗り方針を読んだ"),
        "--eta",
        flags.get("eta", "10m"),
        "--found",
        flags.get("found", "busy"),
        "--",
        *command,
    )


def joins(tmp_path: Path) -> list[str]:
    """掲示板に残っている相乗りの資源 ID。"""
    return [entry.resource for entry in Board(tmp_path).list_all()]


def test_the_wrapper_can_join_instead_of_claiming(tmp_path: Path) -> None:
    """使用中でも相乗りとして入れる。**主宣言は奪わない。**

    実運用で、保持者が相乗り可と申告した GPU に対し malm が claim ではじかれ、
    CPU へ逃げた。案内を足しても rb join には自動解放が無く、ラッパーの
    利点（終了時に必ず外れる）が相乗りだけ得られないままだった。
    """
    hold(tmp_path)

    assert rb_share_run(tmp_path, sys.executable, "-c", "print('ok')") == 0

    # 主宣言はそのまま残っている。
    assert entries(tmp_path) == [normalize("GPU0")]


def test_a_join_made_by_the_wrapper_is_withdrawn_at_the_end(tmp_path: Path) -> None:
    """相乗りは終了時に取り下げられる。

    **相乗りの取り下げ忘れは主宣言より危険である。** 幽霊判定が
    ``since < 起動時刻`` しか無いため、残ると再起動か --force まで消えない。
    """
    hold(tmp_path)

    assert rb_share_run(tmp_path, sys.executable, "-c", "raise SystemExit(3)") == 3

    assert joins(tmp_path) == []


def test_a_join_is_withdrawn_even_when_the_command_is_missing(tmp_path: Path) -> None:
    """起動できなくても相乗りは残らない。"""
    hold(tmp_path)

    assert rb_share_run(tmp_path, "この実行ファイルは存在しない") != 0

    assert joins(tmp_path) == []


def test_joining_requires_a_primary_declaration(tmp_path: Path) -> None:
    """主宣言が無ければ相乗りではなく主宣言を取らせる。"""
    assert rb_share_run(tmp_path, sys.executable, "-c", "print('ok')") == 2

    assert joins(tmp_path) == []
    assert entries(tmp_path) == []


def test_join_and_force_cannot_be_combined(tmp_path: Path) -> None:
    """``--join`` と ``--force`` の併用を弾く。

    --force は「他者の主宣言を退けて取る」で、--join は「退けずに入る」。
    黙ってどちらかに倒すと、相乗りのつもりで他人の宣言を消しうる。
    """
    hold(tmp_path)

    code = run(
        tmp_path,
        "run",
        "--res",
        "GPU0",
        "--share",
        "--force",
        "--job",
        "併用",
        "--observed",
        "調べた",
        "--eta",
        "10m",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )

    assert code == 2
    assert entries(tmp_path) == [normalize("GPU0")]


# --- 掲示板へ書いた後の出力で、申告を孤児にしない -------------------------------


def test_the_wrapper_withdraws_its_own_join_via_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常時は自分の申告を捕獲型 CAS で取り下げる（nonce を照合している）。"""
    hold(tmp_path)
    seen: list[str] = []
    original = Board._capture_and_remove

    def spy(self: Board, path: Path, **kwargs: object) -> object:
        seen.append(str(kwargs.get("expect_nonce")))
        return original(self, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Board, "_capture_and_remove", spy)

    assert rb_share_run(tmp_path, sys.executable, "-c", "print('ok')") == 0

    assert joins(tmp_path) == []
    assert seen, "相乗りの取り下げが CAS を通っていない"


# --- 中断シグナル ----------------------------------------------------------------


def test_terminating_signals_become_an_exception() -> None:
    """POSIX では ``SIGTERM`` が例外になる。**既定のままだと後始末を通らない。**

    子は ``start_new_session=True`` で別のプロセスグループにあるため、シグナルは
    ラッパーにしか届かない。既定のハンドラで死ぬと宣言だけが残り、しかもその
    宣言の PID は死んでいる——**幽霊判定に最も拾われやすい形**で残る。
    """
    if not runner._terminating_signals():
        pytest.skip("シグナルを扱えない環境（Windows）")

    with pytest.raises(runner.Terminated):
        with runner._terminating_signals_raise():
            os.kill(os.getpid(), signal.SIGTERM)


def test_the_previous_signal_handler_is_restored() -> None:
    """区間を抜けたら元のハンドラへ戻す。**他人の設定を奪わない。**"""
    if not runner._terminating_signals():
        pytest.skip("シグナルを扱えない環境（Windows）")

    before = signal.getsignal(signal.SIGTERM)
    with runner._terminating_signals_raise():
        assert signal.getsignal(signal.SIGTERM) is not before

    assert signal.getsignal(signal.SIGTERM) is before


def test_signal_handling_is_a_no_op_where_it_cannot_be_installed() -> None:
    """扱えない環境では**何もせずに通す**（fail-open）。"""
    with runner._terminating_signals_raise():
        pass


# --- 止め切れたかを返す ------------------------------------------------------------


def test_stop_reports_whether_everything_actually_stopped() -> None:
    """``_stop`` は「止め切れたか」を返す。

    捨てると、``SIGKILL`` でも死なない子孫（ドライバ待ちなど）が残ったまま
    「止め終わった」として戻り、呼び出し側が宣言を消す。
    """
    process = FakeProcess()

    assert (
        runner._stop(
            process,
            kill_tree=lambda _pid, _force: True,
            group_alive=lambda _pid: False,
            timeout_s=0.05,
        )
        is True
    )

    assert (
        runner._stop(
            process,
            kill_tree=lambda _pid, _force: True,
            group_alive=lambda _pid: True,  # 何をしても静まらない
            timeout_s=0.05,
        )
        is False
    )


def test_surviving_descendants_are_reported(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """子孫が残ったまま戻るときは黙らない。**待たない**が、言う。

    直接の子が 0 で終わっても孫が資源を掴んだまま生きていることがある。
    呼び出し側の ``finally`` は宣言を消すので、**掲示板は空・資源は掴まれたまま**
    という最も検出しにくい不整合ができる。
    """
    process = FakeProcess()

    monkeypatch.setattr(runner, "_group_alive", lambda _pid: False)
    runner._warn_if_descendants_survive(process, None)
    assert capsys.readouterr().err == "", "生存していないのに警告を出している"

    monkeypatch.setattr(runner, "_group_alive", lambda _pid: True)
    runner._warn_if_descendants_survive(process, None)
    assert "子孫" in capsys.readouterr().err, "生き残りを黙って見逃している"


def test_a_failing_print_does_not_stop_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宣言した後の出力が落ちても、ジョブは走る。

    出す内容には資源 ID とジョブの自由記述が入る。Windows のコンソールは cp932 なので
    ``UnicodeEncodeError`` になりうるし、``| head`` へパイプすれば ``BrokenPipeError``、
    ディスクが一杯なら ``OSError`` になる。ここで例外が漏れると **126 を返し、ジョブは
    1 度も起動しない**——「走らなかったジョブを成功と報告しない」の裏返しである。
    """
    ran: list[str] = []

    def spawn(argv: list[str], log_path: Path, env: dict[str, str]) -> int:
        ran.append(argv[0])
        return 0

    monkeypatch.setattr("resource_broker.cli.SPAWN", spawn)
    real = builtins.print

    def exploding(*args: object, **kwargs: object) -> None:
        text = " ".join(str(a) for a in args)
        if "宣言しました" in text or "ログ:" in text:
            raise BrokenPipeError("パイプが閉じた")
        real(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", exploding)

    code = run(
        tmp_path,
        "run",
        "--res",
        "GPU0",
        "--job",
        "j",
        "--observed",
        "o",
        "--eta",
        "10m",
        "--found",
        "free",
        "--",
        "echo",
        "hi",
    )

    assert ran == ["echo"], "出力が落ちてジョブが走っていない"
    assert code == 0
