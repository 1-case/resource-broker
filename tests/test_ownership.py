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
from resource_broker.board import Board, Entry, LockState, RemovalResult, UpdateResult, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize


def _first(board: Board, resource_id: str) -> Entry | None:
    """その資源の**最初の宣言**（無ければ None）。順序は ``since``。"""
    found = board.list_for(resource_id)
    return found[0] if found else None


def _path_of(board: Board, resource_id: str) -> Path:
    """その資源の宣言が置かれているパス（1 件だけある前提のテスト用）。

    平坦化でファイル名は nonce になったので、**名前から場所を組み立てられない**。
    中身で引く。
    """
    found = board.pairs_for(resource_id)
    assert len(found) == 1, found
    return found[0][0]


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
    assert board.declare(
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
    assert "自分の宣言はありません" in capsys.readouterr().err
    assert _first(board, RESOURCE) is not None


def test_release_with_force_removes_it(tmp_path: Path) -> None:
    """``--force`` があれば消せる。``claim --force`` と対称にする。"""
    board = Board(tmp_path)
    plant(board, THEIRS)

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert _first(board, RESOURCE) is None


def test_release_removes_my_own_declaration(tmp_path: Path) -> None:
    """自分の宣言はそのまま解放できる。"""
    claim(tmp_path)

    assert run(tmp_path, "release", "GPU0") == 0
    assert _first(Board(tmp_path), RESOURCE) is None


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
    declaration = row["declarations"][0]
    assert declaration["usage"]["peak"] == "VRAM 2GB"
    assert declaration["sharing"] == "可"


def test_update_does_not_clobber_a_newer_declaration(tmp_path: Path) -> None:
    """読んでから書くまでに保持者が変わっていたら、古い内容で潰さない。

    照合は nonce で行う。`since` は秒精度なので、同じ秒に解放と再取得が起きると
    別の宣言を同じものと誤認する（この検証で実際に踏んだ）。
    """
    board = Board(tmp_path)
    claim(tmp_path)
    entry = _first(board, RESOURCE)
    assert entry is not None

    # 解放と再取得が起きた状況を作る
    board.remove_all(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    assert (
        board.replace(entry, reason="古い内容", expect_nonce=entry.nonce) is UpdateResult.CONFLICT
    )
    current = _first(board, RESOURCE)
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
    assert board.declare(entry)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("共有違反")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.replace(entry, reason="更新", expect_nonce=entry.nonce) is UpdateResult.FAILED
    assert _first(board, RESOURCE) is not None  # 元の宣言は消えていない


def test_replace_retries_a_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置換も ``unlink`` と同じように数回やり直す。1 回で諦めると更新できない。"""
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(entry)

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
    current = _first(board, RESOURCE)
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
    assert _first(board, RESOURCE) is not None
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
    assert _first(board, RESOURCE) is None


def test_remove_if_owned_refuses_a_reclaimed_declaration(tmp_path: Path) -> None:
    """実行中に他セッションが取り直したら、``rb run`` の後始末は消さない。

    nonce の照合が効いていることを直接確かめる。ここが素通しすると、
    掲示板は空・資源は掴まれたままという最も検出しにくい不整合ができる。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)

    # 他セッションが --force で取り直した状況を作る
    board.remove_all(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    result = board.remove_own(RESOURCE, reason="rb run の終了", nonce=mine.nonce)

    assert result.foreign and not result.removed
    current = _first(board, RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


def test_remove_if_owned_reports_absence_and_success_separately(tmp_path: Path) -> None:
    """「無い」と「消した」を畳まない。

    全部 False に畳むと、``rb run`` が「宣言が自分のものではなくなっています」という
    **事実と違う説明**を出す。
    """
    board = Board(tmp_path)

    assert not board.remove_own(RESOURCE, reason="テスト", nonce="なんでも").any_here

    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)
    assert board.remove_own(RESOURCE, reason="テスト", nonce=mine.nonce).removed


def test_remove_reports_failure_apart_from_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有違反で消せなかったことを「無かった」と混ぜない。

    Windows ではフックが全セッションの全プロンプトで掲示板を読むため、
    ``unlink`` が ``PermissionError`` を返すことが実際にある。
    """
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("共有違反")

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.remove_all(RESOURCE, reason="テスト") == 0


def test_unlink_is_retried_before_giving_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共有違反は数回やり直す。1 回で諦めると宣言が残る。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)

    original = Path.unlink
    attempts = {"n": 0}

    def flaky(self: Path, *args: object, **kwargs: object) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("共有違反")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", flaky)
    monkeypatch.setattr("resource_broker.board.UNLINK_DELAY_S", 0.0)

    assert board.remove_all(RESOURCE, reason="テスト") == 1
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
    assert _first(board, RESOURCE) is not None


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
    assert _first(board, RESOURCE) is not None


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
    assert board.declare(entry)
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
    current = _first(board, RESOURCE)
    assert current is not None
    assert current.job == "B のジョブ"  # B の宣言が生き残っている


def test_conditional_removal_refuses_a_replaced_declaration(tmp_path: Path) -> None:
    """読んだ宣言と違うものは消さない（CAS の基本性質）。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)
    board.remove_all(RESOURCE, reason="テスト")
    plant(board, THEIRS)

    result = board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト")

    assert result is RemovalResult.NOT_OWNED
    current = _first(board, RESOURCE)
    assert current is not None
    assert current.job == "他人のジョブ"


def test_conditional_removal_removes_a_matching_declaration(tmp_path: Path) -> None:
    """一致していれば消す。「無い」と「消した」を畳まない。"""
    board = Board(tmp_path)
    mine = build_entry(RESOURCE, job="私のジョブ", cwd=MINE, session="mine", session_id="mine")
    assert board.declare(mine)

    assert board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト") is (
        RemovalResult.REMOVED
    )
    assert _first(board, RESOURCE) is None
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
    monkeypatch.setattr(Board, "list_for", lambda self, resource_id: [mine])

    assert board.remove_if_nonce(RESOURCE, expect_nonce=mine.nonce, reason="テスト") is (
        RemovalResult.NOT_OWNED
    )

    monkeypatch.undo()
    current = _first(board, RESOURCE)
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
        self: Board,
        resource_id: str,
        *,
        expect_nonce: str,
        reason: str,
        known: tuple[Path, Entry] | None = None,
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
        return original(self, resource_id, expect_nonce=expect_nonce, reason=reason, known=known)

    monkeypatch.setattr(Board, "remove_if_nonce", interleave)
    capsys.readouterr()

    assert run(tmp_path, "release", "GPU0") == 1
    assert "宣言が入れ替わりました" in capsys.readouterr().err
    current = _first(board, RESOURCE)
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
        visible = _first(board, RESOURCE)
        seen["after"] = visible.job if visible is not None else None

    monkeypatch.setattr(os, "link", spy)

    assert board.declare(build_entry(RESOURCE, job="学習", cwd=MINE))

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

    assert board.declare(build_entry(RESOURCE, job="1 本目", cwd=MINE)) is True
    # **2 本目も残る。** ファイル名は nonce なので上書きしない。ハードリンクが
    # 使えない環境でも、作成の退避路（O_EXCL）が同じ結果を出す。
    assert board.declare(build_entry(RESOURCE, job="2 本目", cwd=THEIRS)) is True
    assert sorted(e.job for e in board.list_for(RESOURCE)) == ["1 本目", "2 本目"]


# --- 壊れたエントリからの回復 ---------------------------------------------------


def corrupt(board: Board) -> None:
    """読めないファイルを仕込む。

    平坦化でファイル名は nonce になったので、**壊れたファイルはどの資源のものか
    分からない**。名前で資源を指せないので、置く場所も任意である。
    """
    board.entries_dir.mkdir(parents=True, exist_ok=True)
    (board.entries_dir / "壊れている.json").write_text("{ これは JSON ではない", encoding="utf-8")


def test_a_corrupt_file_blocks_destructive_release_and_is_cleaned_by_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """壊れたファイルは**取得を塞がないが、破壊的な release は止める**。掃除は ``--clean``。

    宣言のファイル名は nonce なので、読めないファイルがあっても新しい宣言は作れる
    ——**取得**は塞がれない。だが**破壊的な操作**（release）は別である。中身が
    読めない以上 ``resource`` フィールドすら分からず、この壊れたファイルが GPU0 の
    ものではないと証明できない（issue #17 指摘 1）。以前は「資源を指すのだから
    無関係な壊れたファイルは見なくてよい」と `--force` が素通ししていたが、それは
    証明できないことを証明できたことにしていた。掃除の道は資源を指定しない
    ``--clean`` だけである。

    ここを取り違えると、次に読む人が「force で消えないのはバグだ」と考えて、
    **資源名からパスを組み立てる削除**を復活させる（それは平坦化が捨てた構造である）。
    """
    board = Board(tmp_path)
    corrupt(board)
    stray = board.unreadable_paths()
    assert len(stray) == 1, stray

    assert claim(tmp_path) == 0, "壊れたファイルが取得を塞いでいる"

    from resource_broker.cli import EXIT_BROKEN

    code = run(tmp_path, "release", "GPU0", "--force")
    assert code == EXIT_BROKEN, "壊れたファイルがあるのに強制解放が完了したと言っている"
    assert board.unreadable_paths() == stray, "拒否したのに壊れたファイルへ触れている"
    assert len(board.list_for(normalize("GPU0"))) == 1, "拒否したのに自分の宣言まで消えている"

    capsys.readouterr()
    assert run(tmp_path, "release", "--clean") == 0
    assert board.unreadable_paths() == [], "--clean で掃除できていない"

    # 掃除した後は、もう拒否する理由が無い。
    assert run(tmp_path, "release", "GPU0", "--force") == 0, "掃除した後もまだ拒否している"


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
    # **壊れたファイルは何も塞がない。** 平坦化でファイル名が nonce になったので、
    # 読めないファイルがあっても新しい宣言は作れる（以前は `board/<資源>.json` が
    # 埋まって、その資源について掲示板が恒久的に機能停止していた）。
    # 残るのは掃除の手段だけなので、場所と掃除の道を示す。
    assert "壊れたエントリ" in captured.err
    assert "--clean" in captured.err
    assert "宣言しました" in captured.out


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
    assert board.declare(entry)

    assert claim(tmp_path, "--found", "free") == 1
    assert claim(tmp_path, "--found", "free", "--force") == 0


def test_boolean_pid_is_rejected(tmp_path: Path) -> None:
    """壊れた掲示板の ``"pid": true`` を PID 1 と解釈しない。"""
    from resource_broker.board import Entry

    entry = Entry.from_dict({"resource": RESOURCE, "holder": {"pid": True}})

    assert entry is not None
    assert entry.pid is None


# --- 相乗りが残った資源 ---------------------------------------------------------


def test_joins_only_resource_is_listed_without_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """引数なしの status でも、相乗りだけの資源を取りこぼさない。"""
    board = Board(tmp_path)
    joiner = build_entry(
        RESOURCE, job="相乗りのジョブ", cwd=THEIRS, session="theirs", session_id="theirs"
    )
    assert board.declare(joiner)

    run(tmp_path, "status", "--json")
    resources = json.loads(capsys.readouterr().out)["resources"]

    assert [r["display"] for r in resources] == ["GPU0"]


# --- join の CLI 経路 -----------------------------------------------------------


# --- 相乗りが消せること ---------------------------------------------------------


def test_force_release_clears_joins_without_a_primary(tmp_path: Path) -> None:
    """主宣言が無くても、残った相乗りを強制解放で掃除できる。"""
    board = Board(tmp_path)
    assert board.declare(build_entry(RESOURCE, job="相乗り", cwd=THEIRS, session_id="theirs"))

    assert run(tmp_path, "release", "GPU0", "--force") == 0
    assert board.list_for(RESOURCE) == []


# --- 強制解放の報告 -------------------------------------------------------------


def shared_primary_and_join(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Board:
    """同じ作業ディレクトリから S の主宣言と T の相乗りが出ている状況を作る。"""
    board = Board(tmp_path)
    shared = str(tmp_path / "同じ場所")
    os.makedirs(shared, exist_ok=True)

    assert board.declare(build_entry(RESOURCE, job="S のジョブ", cwd=shared, session="S"))
    assert board.declare(build_entry(RESOURCE, job="T の相乗り", cwd=shared, session="T"))

    monkeypatch.chdir(shared)
    return board


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
    assert _first(board, RESOURCE) is not None
    assert "自分の宣言はありません" in capsys.readouterr().err


def test_a_second_session_in_the_same_place_cannot_update_my_declaration(
    tmp_path: Path,
) -> None:
    """更新も同じ。他セッションの申告を書き換えない。"""
    board = Board(tmp_path)
    plant(board, MINE, job="先に始めた側の作業", session="first")

    assert run(tmp_path, "update", "GPU0", "--job", "横取り") == 1

    entry = _first(board, RESOURCE)
    assert entry is not None
    assert entry.job == "先に始めた側の作業"


def test_i_can_still_release_my_own_declaration_from_the_same_place(
    tmp_path: Path,
) -> None:
    """自分の宣言は同じ場所からそのまま解放できる。**厳しくしすぎない。**"""
    board = Board(tmp_path)
    plant(board, MINE, job="自分の作業", session="mine")

    assert run(tmp_path, "release", "GPU0") == 0
    assert _first(board, RESOURCE) is None


def test_a_declaration_without_a_session_id_falls_back_to_cwd(
    tmp_path: Path,
) -> None:
    """``session_id`` を持たない宣言は従来どおり cwd で判定する。

    古い宣言や Claude Code 以外からの利用を締め出さない（fail-open）。
    掲示板のスキーマ変更で稼働中のセッションが動かなくなってはならない。
    """
    board = Board(tmp_path)
    assert board.declare(build_entry(RESOURCE, job="session_id の無い宣言", cwd=MINE))

    assert run(tmp_path, "release", "GPU0") == 0
    assert _first(board, RESOURCE) is None


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

    entry = _first(Board(tmp_path), RESOURCE)
    assert entry is not None
    assert entry.holder["session_id"] == "mine"


def test_a_known_nonce_never_falls_back_to_the_directory(tmp_path: Path) -> None:
    """nonce を持っている呼び出し側は、相手に nonce が無ければ**自分のではない**と判定する。

    「照合できないので次の規則へ」と読むと、自分の宣言が外部から強制解放された後に
    同じ場所へ出た**別人の古い形式の宣言**を、後始末が自分のものとして消す。
    """
    board = Board(tmp_path)
    entry = build_entry(RESOURCE, job="他人の古い宣言", cwd=MINE, session_id="")
    entry.holder.pop("nonce", None)

    assert board.owns(entry, nonce="", cwd=MINE) is True  # nonce を持たない操作は従来どおり
    assert board.owns(entry, nonce="自分の nonce", cwd=MINE) is False
