"""fail-open の守護テスト。

本ツールは他の全セッションの Bash 実行経路に割り込む。壊れたときにユーザーの
作業を止めないことが最優先の要件であり、「注意する」だけでは守れない
（CLAUDE.md「Fail-Open」「Testing Constraints」）。

ここでは「壊れた入力を与えても例外が外に出ず、判断材料なしとして扱われる」ことを
網羅的に確認する。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from resource_broker import audit, clock, platform_info
from resource_broker.board import Board, build_entry
from resource_broker.cli import main

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


def test_old_audit_logs_are_pruned(tmp_path: Path) -> None:
    """監査ログを単調増加させない。

    ここには ``job`` の自由記述と宣言元の ``cwd`` が入る——利用者がいつ何の作業を
    していたかの履歴である。``rb wait`` は 10 秒ごとに 1 行足す。放置すれば増え続ける。
    """
    old = audit.audit_dir(tmp_path)
    old.mkdir(parents=True)
    stale = old / "2020-01-01.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    ancient = time.time() - (audit.AUDIT_RETENTION_DAYS + 1) * 86400
    os.utime(stale, (ancient, ancient))

    audit.append(tmp_path, "test_event")

    assert not stale.exists(), "保持期限を過ぎた監査ログが残っている"
    assert list(old.glob("*.jsonl")), "今日のログまで消している"


def test_pruning_does_not_touch_recent_audit_logs(tmp_path: Path) -> None:
    """期限内のログは消さない。**掃除が証拠を消してはならない。**"""
    directory = audit.audit_dir(tmp_path)
    directory.mkdir(parents=True)
    recent = directory / f"{clock.now().date().isoformat()}.jsonl"
    recent.write_text("{}\n", encoding="utf-8")

    assert audit.prune(tmp_path) == 0
    assert recent.exists()


def test_the_age_of_an_audit_log_comes_from_its_name_not_its_mtime(tmp_path: Path) -> None:
    """複製で mtime が新しくなっても、保持期限は効く。

    バックアップからの復元・``robocopy``・クラウド同期・``git checkout`` はいずれも
    mtime を現在時刻へ書き換える。mtime で測ると、**90 日を過ぎた作業履歴が期限を
    跨いで生き残る**。README が「90 日で消える」と書いたことが嘘になる。
    """
    directory = audit.audit_dir(tmp_path)
    directory.mkdir(parents=True)
    copied = directory / "2020-01-01.jsonl"
    copied.write_text("{}\n", encoding="utf-8")  # mtime は今

    assert audit.prune(tmp_path) == 1
    assert not copied.exists()


def test_pruning_records_what_it_deleted(tmp_path: Path) -> None:
    """消したことを 1 行残す。**唯一の追跡手段を無記録で削らない。**

    後日 ``audit/`` の穴を見つけた人が「期限で消えた」「そもそも書かれていない」
    「誰かが手で消した」を区別できるようにする。
    """
    directory = audit.audit_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")

    assert audit.prune(tmp_path) == 1

    written = "".join(path.read_text(encoding="utf-8") for path in directory.glob("*.jsonl"))
    assert "audit_pruned" in written, written


def test_pruning_and_recording_do_not_recurse(tmp_path: Path) -> None:
    """記録が次の掃除を呼び、それがまた記録する——という往復にならない。

    記録は 1 件でも消したときだけ行う。2 回目の掃除では消すものが残っていないので、
    そこで必ず止まる。
    """
    directory = audit.audit_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")

    audit.append(tmp_path, "test_event")

    today = directory / f"{clock.now().date().isoformat()}.jsonl"
    lines = [line for line in today.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sum("audit_pruned" in line for line in lines) == 1, lines


def test_pruning_a_missing_audit_directory_is_not_an_error(tmp_path: Path) -> None:
    """監査ディレクトリが無くても例外を出さない（fail-open）。"""
    assert audit.prune(tmp_path / "無い") == 0


def test_audit_failure_does_not_propagate(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """監査ログが書けなくても本処理は続く。"""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("書けない")

    monkeypatch.setattr(Path, "mkdir", explode)
    board.audit("test_event")  # 例外が出なければ合格


def test_unobserved_resource_is_unknown_not_free() -> None:
    """調べていないことは「空き」ではなく「判定不能」である。

    本ツールは資源を調べない。既定が「空き」に倒れると、誰も調べていない資源を
    全セッションが同時に取れてしまう。
    """
    from resource_broker.liveness import Observation

    assert Observation().busy is None
    assert Observation().known is False


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


def test_claim_proceeds_when_the_session_could_not_investigate(tmp_path: Path) -> None:
    """調べようとして分からなかった場合でも宣言できる。

    「情報がなくて判断できない」ときの既定は**通す**（CLAUDE.md「Fail-Open」）。
    調べられない資源を宣言不能にすると、掲示板そのものが使えなくなる。
    """
    code = main(
        [
            "--home",
            str(tmp_path),
            "claim",
            "GPU0",
            "--job",
            "学習",
            "--observed",
            "nvidia-smi が応答しない",
            "--eta",
            "30m",
            "--found",
            "unknown",
        ]
    )

    assert code == 0


def test_claim_proceeds_when_the_board_directory_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """掲示板が書けなくても、ユーザーの作業を止めない。**0 を返す。**

    宣言は残らないが、それは事故防止の失敗であって作業の停止理由ではない。
    1 を返してよいのは「掲示板が正常に読めた上で使用中と判定できた」場合だけであり、
    書けないことは**インフラの故障**である。ここを 1 に倒すと、掲示板が壊れた瞬間に
    全セッションの資源アクセスが止まる（CLAUDE.md「Fail-Open」）。

    ただし「宣言しました」とは言わない。残っていない宣言を成功と報告すると、
    他セッションから見えないまま「宣言済みのつもり」の状態ができる。
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("書けない")

    monkeypatch.setattr(Path, "mkdir", explode)
    code = main(
        [
            "--home",
            str(tmp_path),
            "claim",
            "GPU0",
            "--job",
            "学習",
            "--observed",
            "compute apps なし",
            "--eta",
            "30m",
            "--found",
            "free",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "宣言しました" not in captured.out
    assert "掲示板に残せていません" in captured.err
    assert "他セッションからは見えません" in captured.err


def test_lock_failure_does_not_look_like_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロックが使えないことを「他セッションが使用中」に化けさせない。

    **インフラの故障と資源の競合を混同しない。** ロックの取得失敗を一律に「使用中」と
    扱うと、掲示板の故障で全セッションの取得が止まる。ロックが使えないときは
    ``O_EXCL`` 一本で従来どおり続行する。
    """
    from contextlib import contextmanager

    from resource_broker.board import Board, LockState

    board = Board(tmp_path)

    @contextmanager
    def unavailable(*_args: object, **_kwargs: object) -> Iterator[LockState]:
        yield LockState.UNAVAILABLE

    monkeypatch.setattr(Board, "locked", unavailable)
    code = main(
        [
            "--home",
            str(tmp_path),
            "claim",
            "GPU0",
            "--job",
            "学習",
            "--observed",
            "調べた",
            "--eta",
            "30m",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "宣言しました" in captured.out
    assert "排他を弱めて続行" in captured.err
    assert board.list_all()  # 宣言は残っている（ロック無しでも取得はできる）
