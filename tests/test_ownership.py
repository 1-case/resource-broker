"""所有と排他の守護テスト。

独立レビューで見つかった穴を固定する。いずれも**掲示板の中核の約束**に関わる。

掲示板が守ると宣言しているのは「先着 1 名」だけである。それが `O_EXCL` 1 箇所で
支えられており、`remove` / `replace` / `release` の 3 経路が素通しできる状態だった。
ここではその 3 経路を含めて、**他人の宣言を消せないこと**と**二重取得が起きないこと**を守る。
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from resource_broker import clock
from resource_broker.board import Board, LockState, RemovalResult, build_entry
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


def test_an_ancestor_directory_does_not_own_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**親ディレクトリは子の宣言を所有しない。**

    このマシンは全プロジェクトが 1 つのルートの下にある。所有を双方向に認めると、
    ハブのルートで動くセッションが**全アセットの宣言を ``--force`` 無しで解放・更新できる**。
    掲示板の唯一の強制点（先着排他）が、上の階層に居るだけで無効になってはならない。
    """
    board = Board(tmp_path)
    plant(board, "C:\\works\\assets\\malm")
    monkeypatch.setattr(os, "getcwd", lambda: "C:\\works")
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0") == 1
    assert board.read(RESOURCE) is not None
    assert run(tmp_path, "update", "GPU0", "--eta", "5m") == 1


def test_a_subdirectory_still_owns_its_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宣言した場所の**配下**へ降りていても、自分の宣言は解放できる。

    宣言時と解放時で作業ディレクトリが違うのは普通にある。そこで弾くと、
    自分の資源を自分で解放できないという最悪の使い勝手になる。
    """
    board = Board(tmp_path)
    plant(board, "C:\\works\\assets\\malm", job="自分のジョブ")
    monkeypatch.setattr(os, "getcwd", lambda: "C:\\works\\assets\\malm\\runs\\e008")

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.read(RESOURCE) is None


def test_remove_if_owned_refuses_a_reclaimed_declaration(tmp_path: Path) -> None:
    """実行中に他セッションが取り直したら、``rb run`` の後始末は消さない。

    nonce の照合が効いていることを直接確かめる。ここが素通しすると、
    掲示板は空・資源は掴まれたままという最も検出しにくい不整合ができる。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine")
    assert board.try_claim(mine)

    # 他セッションが --force で取り直した状況を作る
    board.remove(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    result = board.remove_if_owned(RESOURCE, reason="rb run の終了", nonce=mine.nonce)

    assert result is RemovalResult.NOT_OWNED
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


def test_remove_if_owned_reports_absence_and_success_separately(tmp_path: Path) -> None:
    """「無い」と「消した」を畳まない。

    全部 False に畳むと、``rb run`` が「宣言が自分のものではなくなっています」という
    **事実と違う説明**を出す。
    """
    board = Board(tmp_path)

    assert board.remove_if_owned(RESOURCE, reason="テスト", nonce="なんでも") is (
        RemovalResult.ABSENT
    )

    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine")
    assert board.try_claim(mine)
    assert board.remove_if_owned(RESOURCE, reason="テスト", nonce=mine.nonce) is (
        RemovalResult.REMOVED
    )


def test_remove_reports_failure_apart_from_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有違反で消せなかったことを「無かった」と混ぜない。

    Windows ではフックが全セッションの全プロンプトで掲示板を読むため、
    ``unlink`` が ``PermissionError`` を返すことが実際にある。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine")
    assert board.try_claim(mine)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("共有違反")

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.remove_detailed(RESOURCE, reason="テスト") is RemovalResult.FAILED


def test_unlink_is_retried_before_giving_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有違反は数回やり直す。1 回で諦めると宣言が残る。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine")
    assert board.try_claim(mine)

    original = Path.unlink
    attempts = {"n": 0}

    def flaky(self: Path, *args: object, **kwargs: object) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("共有違反")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", flaky)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.remove_detailed(RESOURCE, reason="テスト") is RemovalResult.REMOVED
    assert attempts["n"] == 3


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


def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    """区間の中で例外が飛んでもロックを残さない。

    残すと、その資源について掲示板が ``LOCK_STALE_S`` のあいだ固まる。
    本ツールの故障でユーザーの作業を止めてはならない。
    """
    board = Board(tmp_path)

    with pytest.raises(RuntimeError):
        with board.locked(RESOURCE) as lock:
            assert lock is LockState.ACQUIRED
            raise RuntimeError("区間の中で壊れた")

    assert not board.lock_path(RESOURCE).exists()


def test_lock_infrastructure_failure_is_not_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ロックが作れないことを「競合」と報告しない。

    **インフラの故障と資源の競合を混同しない**（CLAUDE.md「Fail-Open」）。
    区別しないと、掲示板が壊れた瞬間に全セッションの取得が「使用中」で止まる。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("ロックが作れない")

    monkeypatch.setattr(os, "open", refuse)

    with board.locked(RESOURCE) as lock:
        assert lock is LockState.UNAVAILABLE


def test_a_stale_lock_is_not_stolen_when_it_was_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """奪う直前に中身が入れ替わっていたら奪わない。

    年齢を見てから ``unlink`` するまでの間に、別プロセスが古いロックを消して
    自分のロックを作ることがある。そこで消すと**他人の生きたロック**を消して
    排他が崩れる。トークンが一致するときだけ消す。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    lock = board.lock_path(RESOURCE)
    lock.write_text("古いトークン", encoding="utf-8")
    old = clock.now().timestamp() - 600
    os.utime(lock, (old, old))

    reads = {"n": 0}
    original = board._read_lock_token

    def swap(path: Path) -> str | None:
        reads["n"] += 1
        if reads["n"] == 1:
            return original(path)
        return "新しいトークン"  # 2 回目の確認までに入れ替わった

    monkeypatch.setattr(board, "_read_lock_token", swap)

    assert board._steal_stale_lock(lock, RESOURCE) is False
    assert lock.exists()


def test_a_lock_taken_over_by_another_process_is_not_deleted(tmp_path: Path) -> None:
    """奪われたロックを、元の保持者が返すときに消さない。

    保持が長引いて他プロセスに奪われたあと無条件に ``unlink`` すると、
    他人のロックを消すことになる。自分が書いたトークンと一致するときだけ消す。
    """
    board = Board(tmp_path)

    with board.locked(RESOURCE) as lock:
        assert lock is LockState.ACQUIRED
        # 奪われて、別プロセスが新しいロックを作った状況を作る
        board.lock_path(RESOURCE).write_text("他プロセスのトークン", encoding="utf-8")

    assert board.lock_path(RESOURCE).read_text(encoding="utf-8") == "他プロセスのトークン"


def test_lock_wait_has_an_upper_bound(tmp_path: Path) -> None:
    """待ち時間の上限が効く。

    奪取に成功したときに deadline の評価を飛ばすと、上限が効かなくなる。
    """
    import time

    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    board.lock_path(RESOURCE).write_text("他セッションが保持中", encoding="utf-8")

    started = time.monotonic()
    with board.locked(RESOURCE, wait_s=0.2) as lock:
        assert lock is LockState.CONTENDED
    assert time.monotonic() - started < 2.0


# --- 幽霊判定の境界 -------------------------------------------------------------


def test_future_since_is_uncertain_and_needs_force(tmp_path: Path) -> None:
    """未来の宣言時刻は UNCERTAIN になり、``--force`` でしか退かせない。

    **これは既知の制約であって、解決ではない。** `now - since < grace` は since が
    未来なら常に真になり、`since < boot` も成立しない。時計のずれや手編集で
    そういうエントリができると、猶予を過ぎても退かせない。

    UNCERTAIN は `FREE_VERDICTS` に**入っていない**（掲示板が正常に読めた上で裏が
    取れないだけなので fail-safe に倒す）。ここに判定則を足して自動で退かせるのは、
    「実測が空きでも宣言を退けない」という非対称性を崩す方向なのでやらない
    （CLAUDE.md「Liveness Judgment」）。したがって残る道は `--force` だけである。
    """
    from resource_broker import liveness
    from resource_broker.liveness import Observation, Verdict

    now = clock.now()
    verdict = liveness.judge(
        has_entry=True,
        since=now + timedelta(days=1),
        boot=now - timedelta(hours=1),
        observation=Observation(busy=False),
        pid_alive=False,
        now=now,
    )

    assert verdict == Verdict.UNCERTAIN
    assert liveness.is_free(verdict) is False  # 自動では退かない


def test_future_since_can_only_be_displaced_by_force(tmp_path: Path) -> None:
    """CLI から見ても、未来の since を持つ宣言は ``--force`` でしか退かない。"""
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="時計がずれた宣言", cwd=THEIRS, session="theirs")
    entry.since = clock.to_iso(clock.now() + timedelta(days=1))
    assert board.try_claim(entry)

    assert claim(tmp_path, "--found", "free") == 1
    assert claim(tmp_path, "--found", "free", "--force") == 0


def test_boolean_pid_is_rejected(tmp_path: Path) -> None:
    """壊れた掲示板の ``"pid": true`` を PID 1 と解釈しない。"""
    from resource_broker.board import Entry

    entry = Entry.from_dict({"resource": RESOURCE, "holder": {"pid": True}})

    assert entry is not None
    assert entry.pid is None


# --- 相乗りが残った資源 ---------------------------------------------------------


def test_joins_only_resource_is_occupied_but_claimable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """主宣言が消えて相乗りだけ残った資源は「使用中」だが、主宣言の枠は取れる。

    `free`（主宣言の枠が取れるか）と `occupied`（誰か 1 人でも宣言しているか）は
    別の問いである。混ぜると、status が「使用中」と出しているものを claim が素通しする
    という食い違いが起きる。フックの表示は occupied で絞る（空きと報告すると
    **実際に使っている者がいるのに通知から消える**）。
    """
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)

    run(tmp_path, "status", "GPU0", "--json")
    row = json.loads(capsys.readouterr().out)["resources"][0]

    assert row["occupied"] is True  # 誰かが使っている
    assert row["free"] is True  # 主宣言の枠は空いている
    assert row["has_primary"] is False
    assert row["holders"] == 1
    assert len(row["joins"]) == 1


def test_claim_over_a_joined_resource_warns_but_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """相乗りだけがある資源の claim は、件数を知らせてから通す。

    **止めない。** 相乗りしてよいかを決めるのは当事者であって本ツールではない
    （DESIGN.md「Sharing」）。ただし黙って通すと、掲示板が「使用中」と出している
    ものを CLI が素通しすることになる。
    """
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)
    capsys.readouterr()

    assert claim(tmp_path) == 0
    assert "相乗りが 1 件" in capsys.readouterr().err
    assert board.read(RESOURCE) is not None


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


# --- 相乗りが消せること ---------------------------------------------------------


def test_a_join_from_before_the_reboot_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再起動をまたいだ相乗りは破棄する。

    相乗りには主宣言と違って幽霊を退ける経路が無い。落ちたセッションの申告が
    永久に残ると、その資源は「使用中」に固定され、``rb wait`` は二度と戻らず、
    フックは全セッションの全プロンプトに出し続ける。

    捨てる条件は ``since < 現在の起動時刻`` の 1 つだけである（再起動で全 PID が
    無効になるため推測を含まない）。
    """
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="落ちたセッション", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)

    # 起動時刻が宣言より後 = 再起動をまたいでいる
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() + timedelta(minutes=1)
    )

    assert board.list_joins(RESOURCE) == []
    assert board.join_path(RESOURCE, THEIRS).exists() is False  # 掃除まで行う


def test_a_live_join_is_not_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """起動後に出された相乗りは、猶予も PID も見ずにそのまま残す。

    「実測が空きに見える」「PID が死んでいる」で相乗りを消してはならない
    （CLAUDE.md「Liveness Judgment」の非対称性）。
    """
    board = Board(tmp_path)
    joiner = build_entry(RESOURCE, job="生きている相乗り", cwd=THEIRS, session="theirs")
    assert board.add_join(joiner, THEIRS)

    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() - timedelta(days=1)
    )

    assert len(board.list_joins(RESOURCE)) == 1


def test_force_release_also_clears_the_joins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``release --force`` は相乗りも消す。

    主宣言だけ消せても、残った相乗りが資源を「使用中」に固定し続ける。
    強制解放は最後の掃除手段なので、掃除しきれなければ意味がない。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)
    for place in ("C:\\works\\a", "C:\\works\\b"):
        assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=place), place)
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert "相乗り 2 件" in capsys.readouterr().out
    assert board.read(RESOURCE) is None
    assert board.list_joins(RESOURCE) == []


def test_force_release_clears_joins_without_a_primary(tmp_path: Path) -> None:
    """主宣言が無くても、残った相乗りを強制解放で掃除できる。"""
    board = Board(tmp_path)
    assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=THEIRS), THEIRS)

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert board.list_joins(RESOURCE) == []


def test_a_join_can_be_released_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """申告した場所の配下から ``release`` しても、自分の相乗りを外せる。

    照合を主宣言（``owns``）とそろえる。パスの完全一致にすると、申告時と違う
    ディレクトリから解放したときにキーが変わって外せない。主宣言は祖先関係を
    許すのに相乗りだけ完全一致、という非対称は使う側から説明できない。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)
    place = "C:\\works\\assets\\malm"
    assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=place), place)

    monkeypatch.setattr(os, "getcwd", lambda: place + "\\runs\\e008")

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.list_joins(RESOURCE) == []
    assert board.read(RESOURCE) is not None  # 主宣言は他人のものなので残る


def test_release_does_not_remove_another_sessions_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他セッションの相乗りは ``--force`` 無しでは外せない。"""
    board = Board(tmp_path)
    plant(board, THEIRS)
    assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=THEIRS), THEIRS)

    monkeypatch.setattr(os, "getcwd", lambda: MINE)

    assert run(tmp_path, "release", "GPU0") == 1  # 主宣言も他人のもの
    assert len(board.list_joins(RESOURCE)) == 1
