"""所有と排他の守護テスト。

独立レビューで見つかった穴を固定する。いずれも**掲示板の中核の約束**に関わる。

掲示板が守ると宣言しているのは「先着 1 名」だけである。それが `O_EXCL` 1 箇所で
支えられており、`remove` / `replace` / `release` の 3 経路が素通しできる状態だった。
ここではその 3 経路を含めて、**他人の宣言を消せないこと**と**二重取得が起きないこと**を守る。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from resource_broker import clock
from resource_broker.board import Board, LockState, RemovalResult, UpdateResult, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize

RESOURCE = normalize("GPU0")


@pytest.fixture(autouse=True)
def _my_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """このテストプロセスを「mine」というセッションとして名乗らせる。

    所有判定は cwd だけでは決まらない（同じリポジトリで動く 2 つのセッションは
    cwd も session 名も一致する）。**誰が打ったコマンドなのか**をテストでも明示する。
    """
    monkeypatch.setenv("RESOURCE_BROKER_SESSION_ID", "mine")


#: 互いに**無関係な** 2 つの作業ディレクトリ。階層関係を持たせない。
#:
#: ネイティブの区切りで組み立てる。以前は ``C:\works\mine`` のような Windows 形式の
#: リテラルを実パスとして使っていたが、POSIX では ``\`` は区切りではないため
#: 「区切りの無い 1 つの名前」になり、配下判定が常に False になる。
#: **所有関係のテストが Linux CI で何も検証していなかった**（CI は通っていた）。
MINE = os.path.join(os.sep, "works", "mine")
THEIRS = os.path.join(os.sep, "works", "theirs")


def run(tmp_path: Path, *args: str) -> int:
    return main(["--home", str(tmp_path), *args])


def places(tmp_path: Path, *names: str) -> list[str]:
    """階層関係のある作業ディレクトリを**ネイティブなパス**で組み立てる。

    ``"hub"``, ``"hub/assets/malm"`` のように ``/`` 区切りで書くと、
    実行中の OS の区切りへ変換したパスが返る。**文字列リテラルで階層を書かない**
    （書くと片方の OS でだけ意味を失い、しかもテストは通ってしまう）。
    """
    return [str(tmp_path.joinpath(*name.split("/"))) for name in names]


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


def plant(board: Board, cwd: str, *, job: str = "他人のジョブ", session: str = "theirs") -> None:
    """宣言を仕込む。既定は**他セッションのもの**（``session="mine"`` で自分のものにする）。

    cwd だけでなく ``session_id`` でも身元を分ける。同じ場所で動く 2 つのセッションは
    cwd では区別できないため、そこが所有判定の主材料になっている。
    """
    assert board.try_claim(
        build_entry(RESOURCE, job=job, cwd=cwd, session=session, session_id=session)
    )


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

    assert (
        board.replace(entry, reason="古い内容", expect_nonce=entry.nonce) is UpdateResult.CONFLICT
    )
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


def test_replace_separates_io_failure_from_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置換の I/O 失敗を「宣言が変わった」と説明しない。

    フックが全セッションの全プロンプトで掲示板を読むため、``os.replace`` は共有違反で
    失敗しうる。それを競合として説明するのは**事実と違う**（対処も変わる）。
    """
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.try_claim(entry)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("共有違反")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.replace(entry, reason="更新", expect_nonce=entry.nonce) is UpdateResult.FAILED
    assert board.read(RESOURCE) is not None  # 元の宣言は消えていない


def test_replace_retries_a_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置換も ``unlink`` と同じように数回やり直す。1 回で諦めると更新できない。"""
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.try_claim(entry)

    original = os.replace
    attempts = {"n": 0}

    def flaky(src: object, dst: object, **kwargs: object) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("共有違反")
        original(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    entry.holder["job"] = "書き換え後"
    assert board.replace(entry, reason="更新", expect_nonce=entry.nonce) is UpdateResult.REPLACED
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "書き換え後"


def test_an_ancestor_directory_does_not_own_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**親ディレクトリは子の宣言を所有しない。**

    このマシンは全プロジェクトが 1 つのルートの下にある。所有を双方向に認めると、
    ハブのルートで動くセッションが**全アセットの宣言を ``--force`` 無しで解放・更新できる**。
    掲示板の唯一の強制点（先着排他）が、上の階層に居るだけで無効になってはならない。
    """
    board = Board(tmp_path)
    hub, asset = places(tmp_path, "works", "works/assets/malm")
    plant(board, asset)
    monkeypatch.setattr(os, "getcwd", lambda: hub)
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
    asset, deeper = places(tmp_path, "works/assets/malm", "works/assets/malm/runs/e008")
    plant(board, asset, job="自分のジョブ", session="mine")
    monkeypatch.setattr(os, "getcwd", lambda: deeper)

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.read(RESOURCE) is None


def test_remove_if_owned_refuses_a_reclaimed_declaration(tmp_path: Path) -> None:
    """実行中に他セッションが取り直したら、``rb run`` の後始末は消さない。

    nonce の照合が効いていることを直接確かめる。ここが素通しすると、
    掲示板は空・資源は掴まれたままという最も検出しにくい不整合ができる。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
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

    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
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
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
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
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
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


def test_a_contended_lock_is_not_reported_as_a_busy_resource(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロックを取れなかったことを「使用中」と報告しない。

    ``CONTENDED`` は他セッションが掲示板を**操作中**というだけで、資源が使用中である
    証拠を 1 バイトも含まない（この時点で掲示板をまだ読んでいない）。ここで 1 を返すと
    2 つの嘘をつく。他セッションが ``release`` している最中——**資源が今まさに空こうとして
    いる瞬間**に「使用中」と答え、ロックを持ったままプロセスが死ねば ``LOCK_STALE_S``
    のあいだ**全セッションの取得が「使用中」で止まる**。本ツールの故障を資源の競合として
    報告する形であり、CLAUDE.md「Fail-Open」が名指しで禁じている。

    排他は落ちない。正しさは nonce の CAS と ``try_claim`` の ``O_EXCL`` が担保しており、
    本当に競っていれば ``try_claim`` が負けて**真の busy** が返る
    （``test_two_sessions_seeing_one_ghost_do_not_both_acquire``）。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    board.lock_path(RESOURCE).write_text("99999", encoding="utf-8")  # 他セッションが保持中

    assert claim(tmp_path) == 0
    assert "排他を弱めて続行" in capsys.readouterr().err
    assert board.read(RESOURCE) is not None


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

    共有違反は時間切れになるまでやり直すため、待ち時間を短くして呼ぶ。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("ロックが作れない")

    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    with board.locked(RESOURCE, wait_s=0.05) as lock:
        assert lock is LockState.UNAVAILABLE


def test_a_transient_sharing_violation_does_not_disable_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一時的な共有違反でロックを手放さない。

    Windows の ``os.open(O_CREAT|O_EXCL)`` は、対象が削除待ち（delete-pending）のときや
    他プロセス（AV スキャナを含む）が掴んでいるとき ``FileExistsError`` ではなく
    ``PermissionError`` を返す。これを即座に「ロックが使えない」に落とすと、
    **競合が最も激しい瞬間にロックが外れる**という最悪の相関になる。
    """
    board = Board(tmp_path)
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    original = os.open
    attempts = {"n": 0}

    def flaky(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        if str(path).endswith(".lock"):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("delete-pending")
        return original(path, flags, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", flaky)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    with board.locked(RESOURCE, wait_s=1.0) as lock:
        assert lock is LockState.ACQUIRED
    assert attempts["n"] >= 3


def test_an_empty_lock_file_is_not_treated_as_mine(tmp_path: Path) -> None:
    """空のロックファイルを自分のものとみなさない。

    ``locked`` は ``os.open`` と ``os.write(token)`` の間、ロックファイルが空である。
    奪取された旧保持者がその窓で解放へ来ると、空を自分のものとみなして
    **新しい保持者のロックを消す**。空は「自分のものではない」に倒す
    （書けなかった自分のロックは 30 秒後に steal で回収される）。
    """
    board = Board(tmp_path)

    with board.locked(RESOURCE) as lock:
        assert lock is LockState.ACQUIRED
        # 奪われ、新しい保持者がまだトークンを書いていない状態を作る
        board.lock_path(RESOURCE).write_text("", encoding="utf-8")

    assert board.lock_path(RESOURCE).exists()


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


# --- ロックが無くても二重取得が起きないこと（nonce の CAS） ---------------------


def ghost(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """再起動をまたいだ幽霊を仕込む（確定的に退かせる宣言）。"""
    entry = build_entry(
        RESOURCE, job="落ちたセッション", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    entry.since = clock.to_iso(clock.now() - timedelta(hours=2))
    assert board.try_claim(entry)
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() - timedelta(hours=1)
    )


def test_two_sessions_seeing_one_ghost_do_not_both_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**ロックが使えなくても二重取得は起きない。**

    A と B が同じ幽霊を見て、A が「消して取る」に成功した直後に B が消すと、
    B は**A の生きた宣言を消して**自分も取得に成功する。ロックを安全性の主防御に
    据えている限り、ロックが外れた瞬間（Windows では競合が激しいときほど起きやすい）
    だけこの穴が開く。

    正しさをロックから外し、**削除を「期待する nonce と一致するときだけ」**にすることで、
    ロックの有無に関係なくこの経路を塞ぐ。ここでは A の「読んだ後・消す前」に B を
    丸ごと走らせて、その順序を再現する。
    """
    board = Board(tmp_path)
    ghost(board, monkeypatch)

    @contextmanager
    def unavailable(*_args: object, **_kwargs: object) -> Iterator[LockState]:
        yield LockState.UNAVAILABLE

    monkeypatch.setattr(Board, "locked", unavailable)

    original = Board.remove_if_nonce
    state = {"nested": False}

    def interleave(
        self: Board, resource_id: str, *, expect_nonce: str, reason: str
    ) -> RemovalResult:
        if not state["nested"]:
            state["nested"] = True
            # B が最初から最後まで走り切る（幽霊を退けて宣言する）
            assert (
                run(
                    tmp_path,
                    "claim",
                    "GPU0",
                    "--job",
                    "B のジョブ",
                    "--observed",
                    "調べた",
                    "--eta",
                    "30m",
                )
                == 0
            )
        return original(self, resource_id, expect_nonce=expect_nonce, reason=reason)

    monkeypatch.setattr(Board, "remove_if_nonce", interleave)

    assert claim(tmp_path) == 1  # A は諦める
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "B のジョブ"  # B の宣言が生き残っている


def test_conditional_removal_refuses_a_replaced_declaration(tmp_path: Path) -> None:
    """読んだ宣言と違うものは消さない（CAS の基本性質）。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.try_claim(mine)
    board.remove(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    result = board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト")

    assert result is RemovalResult.NOT_OWNED
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


def test_conditional_removal_removes_a_matching_declaration(tmp_path: Path) -> None:
    """一致していれば消す。「無い」と「消した」を畳まない。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.try_claim(mine)

    assert board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト") is (
        RemovalResult.REMOVED
    )
    assert board.read(RESOURCE) is None
    assert board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト") is (
        RemovalResult.ABSENT
    )


def test_conditional_removal_restores_what_it_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先読みを通り抜けた入れ替わりでも、捕まえた宣言を消さずに元へ戻す。

    CAS は「捕まえてから中身を確かめる」。確かめた結果が別物だったときに戻さないと、
    **他人の生きた宣言を消したのと同じ**ことになる。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    plant(board, THEIRS)  # 実体は他人のもの

    # 先読みだけが自分の宣言を返す状況（読んだ直後に入れ替わった）を作る
    monkeypatch.setattr(Board, "read", lambda self, resource_id: mine)

    assert board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト") is (
        RemovalResult.NOT_OWNED
    )

    monkeypatch.undo()
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"  # 戻っている


def test_release_does_not_remove_a_declaration_reclaimed_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``release`` の最中に他セッションが取り直しても、**新しい宣言を消さない。**

    掲示板の書き換えは全て CAS に乗せる。手で最もよく打つ ``rb release`` だけが
    「読む → ``owns`` → 無条件 ``unlink``」だと次が成立する。T が ``claim --force`` で
    自分の宣言を作り、S の ``release`` はロックが取れなくても続行して**T の新しい宣言**を
    読む。S と T の cwd が同じなら（同一プロジェクトで 2 セッションが動くのは日常である）
    ``owns`` を通り、**S が T の生きた宣言を消す**。掲示板は空・資源は掴まれたままという、
    最も検出しにくい不整合になる。
    """
    board = Board(tmp_path)
    assert claim(tmp_path) == 0

    @contextmanager
    def unavailable(*_args: object, **_kwargs: object) -> Iterator[LockState]:
        yield LockState.UNAVAILABLE

    monkeypatch.setattr(Board, "locked", unavailable)

    original = Board.remove_if_nonce
    state = {"nested": False}

    def interleave(
        self: Board, resource_id: str, *, expect_nonce: str, reason: str
    ) -> RemovalResult:
        if not state["nested"]:
            state["nested"] = True
            # S が読んだ後・消す前に、T が割り込んで取り直す
            assert (
                run(
                    tmp_path,
                    "claim",
                    "GPU0",
                    "--job",
                    "T のジョブ",
                    "--observed",
                    "調べた",
                    "--eta",
                    "30m",
                    "--force",
                )
                == 0
            )
        return original(self, resource_id, expect_nonce=expect_nonce, reason=reason)

    monkeypatch.setattr(Board, "remove_if_nonce", interleave)
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0") == 1
    assert "宣言が入れ替わりました" in capsys.readouterr().err
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "T のジョブ"  # T の宣言が生き残っている


# --- 作成の原子性 ---------------------------------------------------------------


def test_a_declaration_becomes_visible_only_when_it_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**中身の無いエントリが他プロセスから見えない。**

    ``O_EXCL`` で作ってから書くと、作成と書き込みの間に読んだ側が**空のファイル**を見る。
    取得の排他は崩れないが、フックや ``rb status`` から宣言が一瞬消える。
    先に一時ファイルへ書いてハードリンクで名前を付ければ、名前が見えた瞬間には
    中身が揃っている。``os.link`` を使う唯一の根拠がこれである。
    """
    board = Board(tmp_path)
    original = os.link
    seen: dict[str, object] = {}

    def spy(source: object, target: object, **kwargs: object) -> None:
        seen["before"] = Path(str(target)).exists()  # 名前を付ける前は存在しない
        original(source, target, **kwargs)  # type: ignore[arg-type]
        visible = board.read(RESOURCE)
        seen["after"] = visible.job if visible is not None else None

    monkeypatch.setattr(os, "link", spy)

    assert board.try_claim(build_entry(RESOURCE, job="学習", cwd=MINE))

    assert seen["before"] is False
    assert seen["after"] == "学習"  # 名前が見えた瞬間には中身が揃っている
    assert list(board.entries_dir.glob(".*")) == []  # 一時ファイルを残さない


def test_creation_falls_back_when_hard_links_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ハードリンクが使えなくても、先着 1 名の性質は保つ。"""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("ハードリンクが使えない")

    monkeypatch.setattr(os, "link", refuse)
    board = Board(tmp_path)

    assert board.try_claim(build_entry(RESOURCE, job="1 本目", cwd=MINE)) is True
    assert board.try_claim(build_entry(RESOURCE, job="2 本目", cwd=THEIRS)) is False
    current = board.read(RESOURCE)
    assert current is not None
    assert current.job == "1 本目"


# --- 壊れたエントリからの回復 ---------------------------------------------------


def corrupt(board: Board) -> None:
    """読めないエントリを仕込む。"""
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    board.path_for(RESOURCE).write_text("{ これは JSON ではない", encoding="utf-8")


def test_a_corrupt_declaration_can_be_cleaned_with_force(tmp_path: Path) -> None:
    """壊れた主宣言を ``rb release --force`` で掃除できる。

    以前は ``read`` が None なら消さずに「見つかりませんでした」と答えていた。
    壊れたファイルは status に出ず、wait は即座に解放と答え、claim は ``O_EXCL`` で
    永久に失敗する。**その資源について掲示板が恒久的に機能停止し、人間がファイルを
    手で消すしか回復手段が無かった。**
    """
    board = Board(tmp_path)
    corrupt(board)

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert board.path_for(RESOURCE).exists() is False
    assert claim(tmp_path) == 0  # 掃除したあとは普通に取れる


def test_claim_names_the_corrupt_entry_instead_of_failing_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """壊れたエントリで取得できないとき、掃除の仕方を示す。

    「書けませんでした」とだけ言われると、掲示板が読めないのか壊れているのかが
    分からない。回復手段が案内されない状態を作らない。
    """
    corrupt(Board(tmp_path))
    capsys.readouterr()

    code = claim(tmp_path)
    captured = capsys.readouterr()

    assert code == 0  # 判断材料が無いだけ。作業は止めない（fail-open）
    assert "壊れたエントリ" in captured.err
    assert "--force" in captured.err
    assert "掲示板に残せていません" in captured.err
    assert "宣言しました" not in captured.out


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
    entry = build_entry(
        RESOURCE, job="時計がずれた宣言", cwd=THEIRS, session="theirs", session_id="theirs"
    )
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
    joiner = build_entry(
        RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs", session_id="theirs"
    )
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
    joiner = build_entry(
        RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs", session_id="theirs"
    )
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
    joiner = build_entry(
        RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs", session_id="theirs"
    )
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


def test_join_reports_an_io_failure_apart_from_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """掲示板に残せなかったことを「既に申告しています」と言わない。

    ``add_join`` の失敗には mkdir・書き込み・ハードリンクの失敗も含まれる。
    それを「既にある」と畳むと、**掲示板に 1 件も残っていないのに利用者を安心させる**。
    他セッションから見えない利用が始まるのは、掲示板が防ごうとしているものそのものである。

    **作業は止めない**（fail-open）。掲示板に書けないのはインフラの故障であって
    資源の競合ではない。
    """
    plant(Board(tmp_path), THEIRS)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("相乗りのディレクトリが作れない")

    monkeypatch.setattr(Path, "mkdir", refuse)
    capsys.readouterr()

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
    captured = capsys.readouterr()

    assert code == 0  # 止めない
    assert "既に相乗りを申告しています" not in captured.out
    assert "掲示板に残せていません" in captured.err
    assert "相乗りを申告しました" not in captured.out


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
    joiner = build_entry(
        RESOURCE, job="落ちたセッション", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    assert board.add_join(joiner, THEIRS)

    # 起動時刻が宣言より後 = 再起動をまたいでいる（境界の余裕より十分に大きく取る）
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() + timedelta(minutes=10)
    )

    assert board.list_joins(RESOURCE) == []
    assert board.join_path(RESOURCE, THEIRS).exists() is False  # 掃除まで行う


def test_a_join_within_the_boot_margin_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動時刻を**わずかに**下回る相乗りは捨てない。

    ``boot`` は起動からの経過時間から逆算した値なので、NTP が時計を前方へ飛ばすと、
    直前に出したばかりの相乗りが起動より前に見える。境界に余裕を持たせて、
    生きている申告を巻き込む余地をゼロにする。失うのは再起動直後の掃除の遅れだけである。
    """
    board = Board(tmp_path)
    joiner = build_entry(
        RESOURCE, job="出したばかりの相乗り", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    assert board.add_join(joiner, THEIRS)

    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() + timedelta(seconds=30)
    )

    assert len(board.list_joins(RESOURCE)) == 1


def test_a_stale_join_is_removed_only_when_it_still_matches(tmp_path: Path) -> None:
    """相乗りの削除も **nonce の CAS** に乗る（読み取りと削除の間の入れ替わり）。

    古い相乗りを読んだ後、消す前に別プロセスが同じ ``(資源, cwd)`` を取り下げて
    出し直すことがある。無条件の ``unlink`` はその**新しい生きた申告**を消す。
    主宣言で塞いだのと同じ read-delete race である。
    """
    board = Board(tmp_path)
    old = build_entry(
        RESOURCE, job="古い相乗り", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    assert board.add_join(old, THEIRS)

    # 読み終えた直後に、同じ場所の申告が取り下げられて出し直された状況を作る
    board.join_path(RESOURCE, THEIRS).unlink()
    assert board.add_join(build_entry(RESOURCE, job="新しい相乗り", cwd=THEIRS), THEIRS)

    result = board._capture_and_remove(
        board.join_path(RESOURCE, THEIRS),
        expect_nonce=old.nonce,
        resource_id=RESOURCE,
        reason="テスト",
    )

    assert result is RemovalResult.NOT_OWNED
    assert [join.job for join in board.list_joins(RESOURCE)] == ["新しい相乗り"]


def test_stale_join_cleanup_goes_through_the_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読み取り時の相乗り掃除が、無条件 ``unlink`` ではなく CAS を通ること。

    ここが素通しに戻ると、上のテストが守っている性質は経路ごと迂回される。
    """
    board = Board(tmp_path)
    joiner = build_entry(
        RESOURCE, job="落ちたセッション", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    assert board.add_join(joiner, THEIRS)
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() + timedelta(minutes=10)
    )

    seen: list[str] = []
    original = Board._capture_and_remove

    def spy(
        self: Board,
        path: Path,
        *,
        expect_nonce: str,
        resource_id: str,
        reason: str,
        **rest: object,
    ) -> RemovalResult:
        seen.append(expect_nonce)
        return original(
            self, path, expect_nonce=expect_nonce, resource_id=resource_id, reason=reason
        )

    monkeypatch.setattr(Board, "_capture_and_remove", spy)

    assert board.list_joins(RESOURCE) == []
    assert seen == [joiner.nonce]


def test_a_stale_join_that_cannot_be_removed_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """掃除に失敗した相乗りを「無い」と答えない。

    消せていないものを消えたことにすると、資源が空きに見えて他セッションが取りにくる。
    掲示板が出しうる誤りのうち最も危ないのがこれである。
    """
    board = Board(tmp_path)
    assert board.add_join(build_entry(RESOURCE, job="消せない相乗り", cwd=THEIRS), THEIRS)
    monkeypatch.setattr(
        "resource_broker.platform_info.boot_time", lambda: clock.now() + timedelta(minutes=10)
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("共有違反")

    monkeypatch.setattr(os, "rename", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert [join.job for join in board.list_joins(RESOURCE)] == ["消せない相乗り"]


def test_a_live_join_is_not_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """起動後に出された相乗りは、猶予も PID も見ずにそのまま残す。

    「実測が空きに見える」「PID が死んでいる」で相乗りを消してはならない
    （CLAUDE.md「Liveness Judgment」の非対称性）。
    """
    board = Board(tmp_path)
    joiner = build_entry(
        RESOURCE, job="生きている相乗り", cwd=THEIRS, session="theirs", session_id="theirs"
    )
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
    for place in places(tmp_path, "works/a", "works/b"):
        assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=place), place)
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert "相乗り 2 件" in capsys.readouterr().out
    assert board.read(RESOURCE) is None
    assert board.list_joins(RESOURCE) == []


def test_force_release_clears_joins_without_a_primary(tmp_path: Path) -> None:
    """主宣言が無くても、残った相乗りを強制解放で掃除できる。"""
    board = Board(tmp_path)
    assert board.add_join(
        build_entry(RESOURCE, job="相乗り", cwd=THEIRS, session_id="theirs"), THEIRS
    )

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
    place, deeper = places(tmp_path, "works/assets/malm", "works/assets/malm/runs/e008")
    assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=place), place)

    monkeypatch.setattr(os, "getcwd", lambda: deeper)

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.list_joins(RESOURCE) == []
    assert board.read(RESOURCE) is not None  # 主宣言は他人のものなので残る


def test_release_does_not_remove_another_sessions_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他セッションの相乗りは ``--force`` 無しでは外せない。"""
    board = Board(tmp_path)
    plant(board, THEIRS)
    assert board.add_join(
        build_entry(RESOURCE, job="相乗り", cwd=THEIRS, session_id="theirs"), THEIRS
    )

    monkeypatch.setattr(os, "getcwd", lambda: MINE)

    assert run(tmp_path, "release", "GPU0") == 1  # 主宣言も他人のもの
    assert len(board.list_joins(RESOURCE)) == 1


def test_release_prefers_my_own_join_over_one_declared_from_an_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自分の場所から出した相乗りがあるなら、それを外す。

    祖先関係だけで選ぶと、**宣言者の cwd が自分の祖先でありさえすればどれでも一致する**。
    このマシンでは全プロジェクトが 1 つのルートの下にあるため、ハブのルートから出された
    他人の相乗りを配下の全セッションが外せてしまう。完全一致を先に試して塞ぐ。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)
    hub, mine = places(tmp_path, "works", "works/assets/malm")
    assert board.add_join(build_entry(RESOURCE, job="ハブの相乗り", cwd=hub), hub)
    assert board.add_join(build_entry(RESOURCE, job="私の相乗り", cwd=mine), mine)

    monkeypatch.setattr(os, "getcwd", lambda: mine)

    assert run(tmp_path, "release", "GPU0") == 0
    assert [join.job for join in board.list_joins(RESOURCE)] == ["ハブの相乗り"]


def test_release_picks_the_nearest_ancestor_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完全一致が無いときは**最も近い祖先**の相乗りを外す。

    申告時と違うディレクトリから解放するのは普通にある。そのときに「祖先ならどれでも」
    にすると、より浅い場所から出された他人の申告を消しうる。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)
    hub, near, deeper = places(
        tmp_path, "works", "works/assets/malm", "works/assets/malm/runs/e008"
    )
    assert board.add_join(build_entry(RESOURCE, job="ハブの相乗り", cwd=hub), hub)
    assert board.add_join(build_entry(RESOURCE, job="近い相乗り", cwd=near), near)

    monkeypatch.setattr(os, "getcwd", lambda: deeper)

    assert run(tmp_path, "release", "GPU0") == 0
    assert [join.job for join in board.list_joins(RESOURCE)] == ["ハブの相乗り"]


def test_release_keeps_an_ancestors_join_when_i_have_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自分が相乗りしていないなら、祖先から出された**他人の**相乗りは外さない。

    相乗りを先に見ると、ハブのルートから出された申告が ``owns`` の祖先規則を通り、
    配下のセッションの ``rb release`` がそれを選んで消す。しかも早期 return するため、
    **他人の申告だけが消え、呼び出し側自身の主宣言は解放されないまま残る**という
    二重の誤りになる。主宣言を先に見れば、どちらも起きない。
    """
    board = Board(tmp_path)
    hub, mine = places(tmp_path, "works", "works/assets/foo")
    monkeypatch.setattr(os, "getcwd", lambda: mine)
    assert claim(tmp_path) == 0  # 主宣言は自分のもの
    assert board.add_join(build_entry(RESOURCE, job="ハブの相乗り", cwd=hub), hub)

    assert run(tmp_path, "release", "GPU0") == 0

    assert board.read(RESOURCE) is None  # 自分の主宣言が解放されている
    assert [join.job for join in board.list_joins(RESOURCE)] == ["ハブの相乗り"]


# --- 強制解放の報告 -------------------------------------------------------------


def test_force_release_reports_the_joins_even_when_the_primary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """主宣言を消せなくても、相乗りを消した事実は報告する。

    早期 return で握りつぶすと「相乗り N 件を消した」が出力から消え、読んだ側は
    掲示板の状態を誤って把握する。
    """
    board = Board(tmp_path)
    plant(board, THEIRS)
    assert board.add_join(build_entry(RESOURCE, job="相乗り", cwd=MINE), MINE)
    primary_path = board.path_for(RESOURCE)
    original = Path.unlink

    def refuse_primary(self: Path, *args: object, **kwargs: object) -> None:
        if self == primary_path:
            raise PermissionError("共有違反")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse_primary)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    captured = capsys.readouterr()

    assert "解放に失敗しました" in captured.err
    assert "相乗り 1 件" in captured.err


def shared_primary_and_join(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Board:
    """同じ作業ディレクトリから S の主宣言と T の相乗りが出ている状況を作る。"""
    board = Board(tmp_path)
    shared = str(tmp_path / "同じ場所")
    os.makedirs(shared, exist_ok=True)

    assert board.try_claim(build_entry(RESOURCE, job="S のジョブ", cwd=shared, session="S"))
    assert board.add_join(build_entry(RESOURCE, job="T の相乗り", cwd=shared, session="T"), shared)

    monkeypatch.chdir(shared)
    return board


def test_release_stops_when_both_a_primary_and_a_join_are_mine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """主宣言と相乗りの両方が候補になったら、**何も消さずに指定を求める**。

    ここは 5 周にわたって同じ振り子を往復した場所である。相乗りを先に見れば他人の申告を
    消し、主宣言を先に見れば他人の主宣言を消し、完全一致の相乗りを最優先にすれば
    主宣言者が ``release`` を打っても相乗りだけが消える。**cwd だけでは S と T を
    区別できない**以上、どれを選んでも片方が壊れる。

    したがって自動で選ぶのをやめる。掲示板の情報で決められないことを推測しない、
    というのがこの節の唯一の出口である。
    """
    board = shared_primary_and_join(tmp_path, monkeypatch)
    capsys.readouterr()

    code = run(tmp_path, "release", "GPU0")
    captured = capsys.readouterr()

    assert code == 2  # 引数の不備として返す（資源の競合ではない）
    assert "どちらを外すか指定してください" in captured.out
    assert "--primary" in captured.out and "--join" in captured.out
    # **どちらも消していない。** 曖昧なまま片方を消すのが、5 周続いた失敗の形である
    survivor = board.read(RESOURCE)
    assert survivor is not None and survivor.job == "S のジョブ"
    assert [join.job for join in board.list_joins(RESOURCE)] == ["T の相乗り"]


def test_release_can_target_the_primary_or_the_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--join`` は相乗りだけを、``--primary`` は主宣言だけを外す。

    S（主宣言者）と T（相乗り者）が同じ場所に居るとき、**それぞれが自分のものだけを
    外せる**ことが要件である。どちらか一方しか外せない実装は、片方のセッションから見て
    「自分の宣言が解放できない」か「他人の宣言を消してしまう」のどちらかになる。
    """
    board = shared_primary_and_join(tmp_path, monkeypatch)

    # T が自分の相乗りを取り下げる
    assert run(tmp_path, "release", "GPU0", "--join") == 0
    assert board.list_joins(RESOURCE) == []
    survivor = board.read(RESOURCE)
    assert survivor is not None and survivor.job == "S のジョブ", "S の主宣言が消えている"

    # S が自分の主宣言を解放する
    assert run(tmp_path, "release", "GPU0", "--primary") == 0
    assert board.read(RESOURCE) is None


def test_release_primary_does_not_fall_back_to_the_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--primary`` を指定したら、相乗りには手を出さない。

    指定したものが無かったときに黙って別のものを消すと、明示指定の意味が消える。
    """
    board = shared_primary_and_join(tmp_path, monkeypatch)
    assert board.remove(RESOURCE, reason="テスト")  # 主宣言だけ先に消えた状況

    assert run(tmp_path, "release", "GPU0", "--primary") == 0
    assert [join.job for join in board.list_joins(RESOURCE)] == ["T の相乗り"]


def test_release_join_does_not_fall_back_to_the_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--join`` を指定したら、主宣言には手を出さない。"""
    board = shared_primary_and_join(tmp_path, monkeypatch)
    assert board.remove_joins(RESOURCE, reason="テスト") == 1  # 相乗りだけ先に消えた状況

    assert run(tmp_path, "release", "GPU0", "--join") == 0
    survivor = board.read(RESOURCE)
    assert survivor is not None and survivor.job == "S のジョブ"


def test_release_still_chooses_automatically_when_only_one_candidate_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候補が 1 つしか無いときは従来どおり自動で選ぶ。

    曖昧でない場面まで指定を求めると、最もよく打つコマンドが毎回落ちる。
    止めるのは「決められないとき」だけである。
    """
    board = Board(tmp_path)
    place = str(tmp_path / "私の場所")
    os.makedirs(place, exist_ok=True)
    monkeypatch.chdir(place)
    plant(board, THEIRS)  # 主宣言は他人のもの
    assert board.add_join(build_entry(RESOURCE, job="私の相乗り", cwd=place), place)

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.list_joins(RESOURCE) == []
    assert board.read(RESOURCE) is not None  # 他人の主宣言はそのまま


# --- 同じ場所で動く 2 つのセッション ---------------------------------------------
#
# ここが cwd では守れない唯一の場所である。同じリポジトリで実装用とレビュー用の
# セッションを立てると、両者は cwd も `session`（cwd のベース名）も一致する。
# session_id が無いと、**自分の宣言を消したつもりで他セッションの宣言を消す**。


def test_a_second_session_in_the_same_place_cannot_release_my_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ作業ディレクトリの別セッションは ``--force`` 無しで解放できない。"""
    board = Board(tmp_path)
    plant(board, MINE, job="先に始めた側の作業", session="first")

    assert run(tmp_path, "release", "GPU0") == 1  # 自分は "mine"
    assert board.read(RESOURCE) is not None
    assert "他セッションの宣言は解放できません" in capsys.readouterr().err


def test_a_second_session_in_the_same_place_cannot_update_my_declaration(
    tmp_path: Path,
) -> None:
    """更新も同じ。他セッションの申告を書き換えない。"""
    board = Board(tmp_path)
    plant(board, MINE, job="先に始めた側の作業", session="first")

    assert run(tmp_path, "update", "GPU0", "--job", "横取り") == 1

    entry = board.read(RESOURCE)
    assert entry is not None
    assert entry.job == "先に始めた側の作業"


def test_i_can_still_release_my_own_declaration_from_the_same_place(
    tmp_path: Path,
) -> None:
    """自分の宣言は同じ場所からそのまま解放できる。**厳しくしすぎない。**"""
    board = Board(tmp_path)
    plant(board, MINE, job="自分の作業", session="mine")

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.read(RESOURCE) is None


def test_a_declaration_without_a_session_id_falls_back_to_cwd(
    tmp_path: Path,
) -> None:
    """``session_id`` を持たない宣言は従来どおり cwd で判定する。

    古い宣言や Claude Code 以外からの利用を締め出さない（fail-open）。
    掲示板のスキーマ変更で稼働中のセッションが動かなくなってはならない。
    """
    board = Board(tmp_path)
    assert board.try_claim(build_entry(RESOURCE, job="session_id の無い宣言", cwd=MINE))

    assert run(tmp_path, "release", "GPU0") == 0
    assert board.read(RESOURCE) is None


def test_my_own_session_id_is_recorded_on_the_board(tmp_path: Path) -> None:
    """宣言に自分の session_id が載る。載らなければ次回の判定材料が無い。"""
    assert (
        run(
            tmp_path,
            "claim",
            "GPU0",
            "--job",
            "宣言",
            "--observed",
            "調べた",
            "--eta",
            "10m",
        )
        == 0
    )

    entry = Board(tmp_path).read(RESOURCE)
    assert entry is not None
    assert entry.holder["session_id"] == "mine"
