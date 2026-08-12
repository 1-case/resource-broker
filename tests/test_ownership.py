"""所有と排他の守護テスト。

独立レビューで見つかった穴を固定する。いずれも**掲示板の中核の約束**に関わる。

掲示板が守ると宣言しているのは「先着 1 名」だけである。それが `O_EXCL` 1 箇所で
支えられており、`remove` / `replace` / `release` の 3 経路が素通しできる状態だった。
ここではその 3 経路を含めて、**他人の宣言を消せないこと**と**二重取得が起きないこと**を守る。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from resource_broker import clock
from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize

RESOURCE = normalize("GPU0")
MINE = "C:\\works\\mine"
THEIRS = "C:\\works\\theirs"


def run(tmp_path: Path, *args: str) -> int:
    return main(["--home", str(tmp_path), *args])


def claim(tmp_path: Path, *extra: str) -> int:
    return run(
        tmp_path,
        "claim",
        "GPU0",
        "--job",
        "私のジョブ",
        "--observed",
        "調べた",
        "--eta",
        "30m",
        *extra,
    )


def plant(board: Board, cwd: str, *, job: str = "他人のジョブ") -> None:
    """他セッションの宣言を仕込む。"""
    assert board.try_claim(build_entry(RESOURCE, job=job, cwd=cwd, session="theirs"))


# --- 他人の宣言を消せないこと ---------------------------------------------------


def test_release_refuses_another_sessions_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rb release`` は他セッションの宣言を消さない。

    掲示板の唯一の強制点（先着排他）が、最も打ちやすいコマンドで無効化されてはならない。
    「掃除しておこう」と判断したセッションが、他人の生きた宣言を消す経路を塞ぐ。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)

    assert run(tmp_path, "release", "GPU0") == 1
    assert "他セッションの宣言は解放できません" in capsys.readouterr().err
    assert board.read(RESOURCE) is not None


def test_release_with_force_removes_it(tmp_path: Path) -> None:
    """``--force`` があれば消せる。``claim --force`` と対称にする。"""
    board = Board(tmp_path)
    plant(board, THEIRS)

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert board.read(RESOURCE) is None


def test_release_removes_my_own_declaration(tmp_path: Path) -> None:
    """自分の宣言はそのまま解放できる。"""
    claim(tmp_path)

    assert run(tmp_path, "release", "GPU0") == 0
    assert Board(tmp_path).read(RESOURCE) is None


def test_update_refuses_another_sessions_declaration(tmp_path: Path) -> None:
    """他セッションの宣言は書き換えない。誰の言葉か分からなくなる。"""
    plant(Board(tmp_path), THEIRS)

    assert run(tmp_path, "update", "GPU0", "--eta", "5m") == 1


def test_update_rewrites_my_own_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自分の宣言は書き換えられる。進行に応じて実態へ寄せるための機能である。"""
    claim(tmp_path, "--peak", "VRAM 6GB")
    capsys.readouterr()

    assert run(tmp_path, "update", "GPU0", "--peak", "VRAM 2GB", "--sharing", "可") == 0
    capsys.readouterr()

    run(tmp_path, "status", "GPU0", "--json")
    row = json.loads(capsys.readouterr().out)["resources"][0]
    assert row["usage"]["peak"] == "VRAM 2GB"
    assert row["sharing"] == "可"


def test_update_does_not_clobber_a_newer_declaration(tmp_path: Path) -> None:
    """読んでから書くまでに保持者が変わっていたら、古い内容で潰さない。

    照合は nonce で行う。`since` は秒精度なので、同じ秒に解放と再取得が起きると
    別の宣言を同じものと誤認する（この検証で実際に踏んだ）。
    """
    board = Board(tmp_path)
    claim(tmp_path)
    entry = board.read(RESOURCE)
    assert entry is not None

    # 解放と再取得が起きた状況を作る
    board.remove(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    assert board.replace(entry, reason="古い内容", expect_nonce=entry.nonce) is False
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


# --- 二重取得が起きないこと -----------------------------------------------------


def test_lock_blocks_a_second_acquisition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """取得の排他区間が守られている。

    ``O_EXCL`` が守るのは作成だけで、「読んで、幽霊なら消して、作る」の途中は無防備だった。
    2 つのセッションが同じ幽霊を見て、両方とも `remove` → `try_claim` に成功しうる。
    ロックが取れないときは**取得を諦める**（通すと二重取得になる）。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    board.lock_path(RESOURCE).write_text("99999", encoding="utf-8")  # 他セッションが保持中

    assert claim(tmp_path) == 1
    assert "操作中" in capsys.readouterr().err
    assert board.read(RESOURCE) is None


def test_stale_lock_is_stolen(tmp_path: Path) -> None:
    """放置されたロックは奪う。

    ロックを持ったままプロセスが死ぬと掲示板がその資源について永久に固まる。
    本ツールの故障でユーザーの作業を止めてはならない。
    """
    import os

    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    lock = board.lock_path(RESOURCE)
    lock.write_text("99999", encoding="utf-8")
    old = clock.now().timestamp() - 600
    os.utime(lock, (old, old))

    assert claim(tmp_path) == 0
    assert board.read(RESOURCE) is not None


def test_lock_is_released_after_claim(tmp_path: Path) -> None:
    """取得が終わったらロックを残さない。"""
    claim(tmp_path)

    assert not Board(tmp_path).lock_path(RESOURCE).exists()


# --- 幽霊判定の境界 -------------------------------------------------------------


def test_future_since_does_not_become_immortal(tmp_path: Path) -> None:
    """未来の宣言時刻を持つエントリが永久に残らない。

    `now - since < grace` は since が未来なら常に真になり、`since < boot` も成立しない。
    時計のずれや手編集で、`--force` 以外に退かせない宣言ができてしまう。
    """
    from resource_broker import liveness
    from resource_broker.liveness import Observation, Verdict

    now = clock.now()
    verdict = liveness.judge(
        has_entry=True,
        since=now + timedelta(days=1),
        boot=now - timedelta(hours=1),
        observation=Observation(),
        pid_alive=None,
        now=now,
    )

    assert verdict == Verdict.UNCERTAIN


def test_boolean_pid_is_rejected(tmp_path: Path) -> None:
    """壊れた掲示板の ``"pid": true`` を PID 1 と解釈しない。"""
    from resource_broker.board import Entry

    entry = Entry.from_dict({"resource": RESOURCE, "holder": {"pid": True}})

    assert entry is not None
    assert entry.pid is None


# --- 相乗りが残った資源 ---------------------------------------------------------


def test_joins_only_resource_is_not_reported_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """主宣言が消えて相乗りだけ残った資源を「空き」と言わない。

    フックの表示はこの値で絞られるため、空きと報告すると**実際に使っている者がいるのに
    通知から消える**。
    """
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)

    run(tmp_path, "status", "GPU0", "--json")
    row = json.loads(capsys.readouterr().out)["resources"][0]

    assert row["free"] is False
    assert row["has_primary"] is False
    assert row["holders"] == 1
    assert len(row["joins"]) == 1


def test_joins_only_resource_is_listed_without_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """引数なしの status でも、相乗りだけの資源を取りこぼさない。"""
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)

    run(tmp_path, "status", "--json")
    resources = json.loads(capsys.readouterr().out)["resources"]

    assert [r["display"] for r in resources] == ["GPU0"]


# --- join の CLI 経路 -----------------------------------------------------------


def test_join_requires_a_primary_declaration(tmp_path: Path) -> None:
    """主宣言が無ければ相乗りではなく claim を使わせる。"""
    code = run(
        tmp_path,
        "join",
        "--res",
        "GPU0",
        "--job",
        "相乗り",
        "--observed",
        "調べた",
        "--eta",
        "10m",
    )

    assert code == 2


def test_join_is_allowed_even_when_found_busy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--found busy`` でも相乗りは申告できる。

    相乗りは「使われている」ことが前提である。claim が逆に止めるのと対になる。
    """
    plant(Board(tmp_path), THEIRS)
    capsys.readouterr()

    code = run(
        tmp_path,
        "join",
        "--res",
        "GPU0",
        "--job",
        "相乗り",
        "--observed",
        "使用中と分かっている",
        "--eta",
        "10m",
        "--found",
        "busy",
    )

    assert code == 0
    assert "相乗りを申告しました" in capsys.readouterr().out


def test_join_twice_from_the_same_place_is_refused(tmp_path: Path) -> None:
    """同じ作業ディレクトリから二重には申告できない。"""
    plant(Board(tmp_path), THEIRS)
    argv = ["join", "--res", "GPU0", "--job", "相乗り", "--observed", "調べた", "--eta", "10m"]

    assert run(tmp_path, *argv) == 0
    assert len(Board(tmp_path).list_joins(RESOURCE)) == 1

    run(tmp_path, *argv)
    assert len(Board(tmp_path).list_joins(RESOURCE)) == 1
