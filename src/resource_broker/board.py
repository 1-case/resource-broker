"""掲示板の読み書き。

掲示板は**資源ごとに 1 ファイル**の JSON である。単一ファイルに集約しないのは、
破損の被害を 1 資源に閉じ込めるためと、``O_EXCL`` による取得競合の解決を
単純にするためである（DESIGN.md「Architecture」）。

**正しさはロックではなく nonce の compare-and-swap（CAS）が担保する。**
掲示板を書き換える操作は「期待する nonce と一致するときだけ消す／置く」の形にしてある。
ロックは競り合いを減らすための性能最適化であり、取れても取れなくても正しさは変わらない
（DESIGN.md「Per-Resource Lock」）。ロックを主防御に据えると、ロックが外れた瞬間だけ
排他が消えるという最悪の相関を持つことになる。Windows では、競合が激しいときほど
``O_EXCL`` が ``PermissionError`` になりやすく、この相関は現実に起こる。

本モジュールの全ての公開関数は**例外を投げない**。読めない・書けない・壊れているは
すべて「情報が無い」に畳み込み、呼び出し側が fail-open で通せるようにする。
握りつぶした事実は監査ログに残す。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from . import audit, clock, naming, platform_info

SCHEMA = 1

#: 取得の排他区間を守るロックを待つ秒数。
#:
#: ロックが要るのは「読んで、幽霊を退けて、作る」の間だけで、保持は数ミリ秒である。
#: ここで待たされるのは他セッションが同じ資源を同時に取りにきたときに限られる。
LOCK_WAIT_S = 2.0

#: ロックが放置されたとみなす秒数。
#:
#: ロックを持ったままプロセスが死ぬと、掲示板がその資源について永久に固まる。
#: 本ツールが壊れてユーザーの作業を止めてはならない（CLAUDE.md「Fail-Open」）ので、
#: 明らかに古いロックは奪う。保持時間はミリ秒単位なので、この閾値は十分に安全側である。
LOCK_STALE_S = 30.0

#: 削除をやり直す回数と間隔。
#:
#: Windows では、他プロセスが読んでいる最中のファイルを消すと共有違反
#: （``PermissionError``）になる。フックが**全セッションの全プロンプト**で掲示板を
#: 読むため、これは例外的な事態ではなく普通に起こりうる。1 回の失敗で諦めると、
#: 解放したはずの宣言が残って資源を占有し続ける。
UNLINK_ATTEMPTS = 4
UNLINK_DELAY_S = 0.05

#: 相乗りを「再起動をまたいだ幽霊」とみなすときの余裕（秒）。
#:
#: 判定は ``since < boot - この余裕`` で行う。``boot`` は起動からの経過時間から
#: 逆算した値なので、NTP が時計を**前方**へ飛ばすと、直前に出したばかりの相乗りが
#: 起動時刻より前に見えることがある。余裕を取っておけばその窓がゼロになる。
#: コストは「再起動直後の 1 分間だけ掃除が遅れる」ことだけである。
JOIN_BOOT_MARGIN_S = 60.0


class LockState(StrEnum):
    """ロックの取得結果。

    **3 値にするのは、インフラの故障と資源の競合を混同しないためである**
    （CLAUDE.md「Fail-Open」）。2 値にすると「ロックのディレクトリが作れない」
    という本ツール側の故障が「他セッションが使用中」に化け、掲示板が壊れた瞬間に
    全セッションの取得が止まる。

    どの値が返っても**正しさは変わらない**。書き換えは nonce の CAS で守ってあり、
    ロックは競り合いを減らすだけだからである。
    """

    ACQUIRED = "acquired"
    """取れた。排他区間に入ってよい。"""

    CONTENDED = "contended"
    """他セッションが保持していて時間内に取れなかった。**排他を弱めて続行する**。

    「掲示板を操作中の者がいる」であって、**資源が使用中である証拠は 1 バイトも無い**
    （この時点で掲示板を読んでいない）。ここで諦めると、他セッションが ``release``
    している最中——資源が今まさに空こうとしている瞬間に「使用中」と答えることになる。"""

    UNAVAILABLE = "unavailable"
    """ロックの仕組みそのものが使えない（作れない・権限が無い・ディスクが一杯）。
    これは資源の競合ではないので、**ロック無しで従来どおり続行する**。"""


class RemovalResult(StrEnum):
    """削除の結果。

    「無い」「他人のもの」「失敗した」を区別する。全部 ``False`` に畳むと、
    共有違反で消せなかっただけなのに「宣言が自分のものではなくなっています」という
    **事実と違う説明**を出すことになる。
    """

    REMOVED = "removed"
    ABSENT = "absent"
    NOT_OWNED = "not_owned"
    FAILED = "failed"


class UpdateResult(StrEnum):
    """置換の結果。

    **競合（nonce 不一致）と I/O 失敗を分ける。** 畳むと「読んでから書くまでに宣言が
    変わった可能性」という説明が共有違反にも付き、**I/O の失敗を競合として説明する**
    ことになる。本コードベースが他所で戒めているのと同じ誤りである。
    """

    REPLACED = "replaced"
    CONFLICT = "conflict"
    FAILED = "failed"


class JoinResult(StrEnum):
    """相乗りの申告の結果。

    **「既にある」と「掲示板に残せなかった」を畳まない。** 畳むと、mkdir・書き込み・
    ハードリンクのいずれが失敗しても「既に申告しています」と答えることになり、
    **掲示板に 1 件も残っていないのに利用者を安心させる**。他セッションから見えない
    利用が始まるのは、掲示板が防ごうとしているもの（衝突）そのものである。
    """

    ADDED = "added"
    EXISTS = "exists"
    FAILED = "failed"


class MoveResult(StrEnum):
    """ファイル移動の結果。CAS の「捕まえる」「戻す」で使う。"""

    MOVED = "moved"
    ABSENT = "absent"
    """元のファイルが無い。**他の誰かが先に動かした**（＝競争に負けた）。"""

    BLOCKED = "blocked"
    """宛先が既にある。"""

    FAILED = "failed"


def _unlink_with_retry(path: Path) -> tuple[RemovalResult, str]:
    """ファイルを消す。共有違反は数回やり直す。

    Returns
    -------
    tuple of (RemovalResult, str)
        結果と、失敗したときの理由。
    """
    for attempt in range(UNLINK_ATTEMPTS):
        try:
            path.unlink()
        except FileNotFoundError:
            return RemovalResult.ABSENT, ""
        except OSError as exc:
            if attempt == UNLINK_ATTEMPTS - 1:
                return RemovalResult.FAILED, str(exc)
            time.sleep(UNLINK_DELAY_S)
            continue
        return RemovalResult.REMOVED, ""
    return RemovalResult.FAILED, "削除を諦めた"


def _rename_with_retry(source: Path, target: Path) -> tuple[MoveResult, str]:
    """ファイルの名前を変える。共有違反は数回やり直す。

    ``os.rename`` は**元のファイルを 1 人だけが取れる**（成功した瞬間に元の名前は
    消えるので、同時に走った他方は ``FileNotFoundError`` になる）。CAS の「捕まえる」
    段階はこの性質だけで成立し、ロックを必要としない。

    Returns
    -------
    tuple of (MoveResult, str)
        結果と、失敗したときの理由。
    """
    for attempt in range(UNLINK_ATTEMPTS):
        try:
            os.rename(source, target)
        except FileNotFoundError:
            return MoveResult.ABSENT, ""
        except FileExistsError:
            return MoveResult.BLOCKED, ""
        except OSError as exc:
            if attempt == UNLINK_ATTEMPTS - 1:
                return MoveResult.FAILED, str(exc)
            time.sleep(UNLINK_DELAY_S)
            continue
        return MoveResult.MOVED, ""
    return MoveResult.FAILED, "移動を諦めた"


def _replace_with_retry(source: Path, target: Path) -> tuple[bool, str]:
    """一時ファイルを本体へ置き換える。共有違反は数回やり直す。

    ``unlink`` と同じ理由でやり直す。フックが**全セッションの全プロンプト**で掲示板を
    読むため、Windows では置換が共有違反で失敗しうる。1 回で諦めると、更新が
    「読んでから書くまでに宣言が変わった」という**事実と違う説明**で落ちる。
    """
    for attempt in range(UNLINK_ATTEMPTS):
        try:
            os.replace(source, target)
        except OSError as exc:
            if attempt == UNLINK_ATTEMPTS - 1:
                return False, str(exc)
            time.sleep(UNLINK_DELAY_S)
            continue
        return True, ""
    return False, "置換を諦めた"


def _read_entry_at(path: Path) -> Entry | None:
    """パスを指定して Entry を読む。読めない・壊れていれば None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return Entry.from_dict(data)


#: 読み取り時に既知として扱うキー。これ以外は extra に退避して書き戻す（前方互換）。
_KNOWN_KEYS = frozenset(
    {
        "schema",
        "resource",
        "display",
        "holder",
        "log",
        "since",
        "boot",
        "observed",
        "eta",
        "usage",
        "sharing",
    }
)


def _is_within(child: str, parent: str) -> bool:
    """``child`` が ``parent`` と同じ場所か、その**配下**か。

    **方向を一方向に限る。** 以前は逆方向（親が子の宣言を所有する）も認めていたが、
    このマシンでは全プロジェクトが 1 つのルートの下にあるため、ハブのルートで動く
    セッションが**全アセットの宣言を ``--force`` 無しで解放・更新できた**。
    「自分の場所の外にある宣言は自分のものではない」という線をここで引く。
    """
    a = os.path.normcase(os.path.normpath(child))
    b = os.path.normcase(os.path.normpath(parent))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


@dataclass
class Entry:
    """掲示板の 1 エントリ。

    全フィールドを機械が生成できることが要件である。人間や LLM にしか
    書けない項目を増やしてはならない（DESIGN.md「Board Schema」）。
    """

    resource: str
    display: str = ""
    holder: dict[str, object] = field(default_factory=dict)
    log: str | None = None
    since: str = ""
    boot: str | None = None
    observed: dict[str, object] | None = None
    eta: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    sharing: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def since_dt(self) -> datetime | None:
        """宣言時刻。解釈できなければ None。"""
        return clock.parse_iso(self.since)

    @property
    def pid(self) -> int | None:
        """宣言に書かれた PID。無ければ None。

        ``bool`` は ``int`` の派生なので明示的に除く。壊れた掲示板に ``"pid": true`` が
        あると PID 1 の生存を確かめにいくことになる。
        """
        value = self.holder.get("pid")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @property
    def nonce(self) -> str:
        """この宣言を一意に識別する値。所有者の照合に使う。"""
        value = self.holder.get("nonce")
        return value if isinstance(value, str) else ""

    @property
    def job(self) -> str:
        """ジョブの説明。"""
        value = self.holder.get("job")
        return value if isinstance(value, str) else ""

    @property
    def session(self) -> str:
        """宣言したセッションの名前。"""
        value = self.holder.get("session")
        return value if isinstance(value, str) else "unknown"

    def to_dict(self) -> dict[str, object]:
        """JSON へ書き出す dict にする。"""
        data: dict[str, object] = dict(self.extra)
        data.update(
            {
                "schema": SCHEMA,
                "resource": self.resource,
                "display": self.display,
                "holder": self.holder,
                "log": self.log,
                "since": self.since,
                "boot": self.boot,
                "observed": self.observed,
                "eta": self.eta,
                "usage": self.usage,
                "sharing": self.sharing,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: object) -> Entry | None:
        """dict から Entry を作る。最低限の形をなしていなければ None。

        未知のキーは ``extra`` に退避して保持する。新しいバージョンが書いた
        フィールドを古いバージョンが消してしまわないようにするためである。
        """
        if not isinstance(data, dict):
            return None
        resource = data.get("resource")
        if not isinstance(resource, str) or not resource:
            return None
        holder = data.get("holder")
        observed = data.get("observed")
        eta = data.get("eta")
        usage = data.get("usage")
        return cls(
            resource=resource,
            display=data["display"] if isinstance(data.get("display"), str) else "",
            holder=holder if isinstance(holder, dict) else {},
            log=data["log"] if isinstance(data.get("log"), str) else None,
            since=data["since"] if isinstance(data.get("since"), str) else "",
            boot=data["boot"] if isinstance(data.get("boot"), str) else None,
            observed=observed if isinstance(observed, dict) else None,
            eta=eta if isinstance(eta, dict) else None,
            usage=usage if isinstance(usage, dict) else None,
            sharing=data["sharing"] if isinstance(data.get("sharing"), str) else "",
            extra={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
        )


class Board:
    """掲示板。

    Parameters
    ----------
    root : Path or str, optional
        掲示板を置くルート。省略時は ``platform_info.board_root()``。
        テストでは一時ディレクトリを渡す（実運用の掲示板を汚さないため）。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else Path(platform_info.board_root())

    @property
    def entries_dir(self) -> Path:
        """エントリを置くディレクトリ。"""
        return self.root / "board"

    @property
    def audit_dir(self) -> Path:
        """監査ログを置くディレクトリ。"""
        return audit.audit_dir(self.root)

    def path_for(self, resource_id: str) -> Path:
        """資源 ID に対応するエントリのパスを返す。"""
        return self.entries_dir / f"{naming.safe_filename(resource_id)}.json"

    def lock_path(self, resource_id: str) -> Path:
        """取得の排他区間を守るロックのパス。"""
        return self.entries_dir / f"{naming.safe_filename(resource_id)}.lock"

    @contextmanager
    def locked(self, resource_id: str, *, wait_s: float = LOCK_WAIT_S) -> Iterator[LockState]:
        """書き換えの区間を囲う。**これは性能最適化であって、安全性の主防御ではない。**

        「読んで、幽霊なら退けて、作る」の途中は ``O_EXCL`` では守れない。囲えば
        同じ幽霊を 2 人が見る場面自体が減るので、無駄な往復が減る。しかし
        **囲えなくても正しさは変わらない**。掲示板の書き換えは nonce の CAS
        （:meth:`remove_if_nonce` / :meth:`replace`）で守ってあり、ロックが外れた瞬間だけ
        排他が消えるということは無い。ロックを主防御に据えると、Windows で
        「競合が激しいときほど ``O_EXCL`` が ``PermissionError`` になる」という
        最悪の相関を抱えることになる。

        結果は 3 値で渡す（:class:`LockState`）。**インフラの故障と資源の競合を
        混同しない**（CLAUDE.md「Fail-Open」）。ただし**どの値でも呼び出し側は止まらない**。
        ロックが取れなかったことは、``CONTENDED`` でも ``UNAVAILABLE`` でも、
        資源が使用中である証拠を含まないからである。3 値に分けるのは、
        どちらの理由で排他が弱まったかを監査ログと説明文で区別するためである。

        放置されたロックは奪う。ロックを持ったままプロセスが死んで掲示板が永久に固まるのは、
        本ツールの故障でユーザーの作業を止めることに等しい。

        Yields
        ------
        LockState
            取得の結果。
        """
        path = self.lock_path(resource_id)
        # 取得ごとに一意なトークン。奪うとき・返すときに「これは自分（または自分が
        # 見た）ロックか」を確かめるために使う。PID では再利用があるため足りない。
        token = uuid.uuid4().hex
        state = LockState.CONTENDED
        deadline = time.monotonic() + wait_s
        stolen = False

        try:
            self.entries_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.audit("lock_mkdir_failed", resource=resource_id, error=str(exc))
            yield LockState.UNAVAILABLE
            return

        while True:
            try:
                handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                # **奪うのは 1 回だけ**。奪っても取れないなら、それは放置ではなく
                # 競合である（奪った直後に別のセッションが取っている）。
                if not stolen:
                    stolen = self._steal_stale_lock(path, resource_id)
                # 奪えたかどうかに関わらず deadline を評価する。ここを飛ばすと
                # 待ち時間の上限が効かなくなる。
                if time.monotonic() >= deadline:
                    self.audit("lock_timeout", resource=resource_id)
                    state = LockState.CONTENDED
                    break
                time.sleep(UNLINK_DELAY_S)
                continue
            except PermissionError as exc:
                # **共有違反を即座に「ロックが使えない」に落とさない。** Windows では、
                # 削除待ち（delete-pending）のファイルや、他プロセス（AV スキャナを含む）が
                # 掴んでいるファイルへの O_EXCL が FileExistsError ではなく
                # PermissionError になる。これは競合が激しいときほど起きやすいため、
                # 即座に諦めると**最も競り合っている瞬間にロックが外れる**。
                # deadline まではやり直し、時間切れになって初めて使えないとみなす。
                if time.monotonic() >= deadline:
                    self.audit("lock_failed", resource=resource_id, error=str(exc))
                    state = LockState.UNAVAILABLE
                    break
                time.sleep(UNLINK_DELAY_S)
                continue
            except OSError as exc:
                # ディスク一杯・パスが作れない等。やり直しても結果は変わらない。
                self.audit("lock_failed", resource=resource_id, error=str(exc))
                state = LockState.UNAVAILABLE
                break

            state = LockState.ACQUIRED
            try:
                os.write(handle, token.encode("ascii"))
            except OSError:
                pass
            finally:
                os.close(handle)
            break

        try:
            yield state
        finally:
            if state is LockState.ACQUIRED:
                self._release_lock(path, token, resource_id)

    def _read_lock_token(self, path: Path) -> str | None:
        """ロックファイルの中身を読む。読めなければ None。"""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _steal_stale_lock(self, path: Path, resource_id: str) -> bool:
        """放置されたロックを奪う。奪ったら True。

        **他人の新しいロックを消さない。** 年齢を見てから ``unlink`` するまでの間に、
        別プロセスが古いロックを消して自分のロックを作ることがある。その隙に消すと、
        生きているロックを消して排他が崩れる。ロックファイルには取得ごとに一意な
        トークンが入っているので、**読んだときと同じ内容である**ことを確かめてから消す。
        """
        first = self._read_lock_token(path)
        if first is None:
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        if age < LOCK_STALE_S:
            return False
        if self._read_lock_token(path) != first:
            return False  # 入れ替わっている。他人の新しいロックである
        result, _ = _unlink_with_retry(path)
        if result is not RemovalResult.REMOVED:
            return False
        self.audit("lock_stolen", resource=resource_id, age_s=round(age, 1))
        return True

    def _release_lock(self, path: Path, token: str, resource_id: str) -> None:
        """自分が取ったロックだけを返す。**自分が書いたトークンと一致するときだけ**消す。

        保持が ``LOCK_STALE_S`` を超えると他プロセスに奪われる。奪われたあとに無条件で
        ``unlink`` すると、**他人のロックを消す**ことになる。

        **空は「自分のものではない」に倒す。** ``locked`` は ``os.open`` と
        ``os.write(token)`` の間、ロックファイルが空である。奪われた旧保持者がその窓で
        ここへ来ると、空を自分のものとみなして**新しい保持者のロックを消す**。
        自分が書けなかったロックは 30 秒後に steal で回収されるので、失うもの
        （30 秒だけ残る）より守るもの（他人のロックを消さない）が大きい。
        """
        current = self._read_lock_token(path)
        if current is None:
            return  # 既に無い、または読めない。触らない
        if current != token:
            self.audit("lock_release_skipped", resource=resource_id)
            return
        _unlink_with_retry(path)

    def read(self, resource_id: str) -> Entry | None:
        """エントリを読む。存在しない・壊れている場合は None。

        **壊れていることと存在しないことを区別しない。** どちらも「情報が無い」
        として扱い、呼び出し側は通す。壊れていた事実は監査ログに残す。
        """
        path = self.path_for(resource_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.audit("read_failed", resource=resource_id, error=str(exc))
            return None

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.audit("entry_corrupt", resource=resource_id, error=str(exc))
            return None

        entry = Entry.from_dict(data)
        if entry is None:
            self.audit("entry_malformed", resource=resource_id)
        return entry

    def owns(self, entry: Entry, *, nonce: str | None = None, cwd: str | None = None) -> bool:
        """そのエントリが自分のものか。

        nonce が一致すれば確実に自分のものである（宣言ごとに一意）。nonce を持たない
        古いエントリのために cwd での照合も残す。

        cwd の照合は**宣言者の場所が自分の場所と同じか、自分の祖先のとき**だけ自分のものと
        みなす。宣言したときと解放するときで作業ディレクトリが違うことは普通にある
        （サブディレクトリへ降りた等）ので、そこで弾くと自分の資源を自分で解放できない。
        逆方向（自分が宣言者の祖先）は**認めない**。このマシンでは全プロジェクトが 1 つの
        ルートの下にあるため、認めるとハブのルートで動くセッションが全アセットの宣言を
        ``--force`` 無しで解放・更新できてしまう。

        **所有者を確かめずに消してはならない。** 他セッションの生きた宣言を消すと、
        掲示板は空・資源は掴まれたままという最も検出しにくい不整合ができる。
        """
        holder = entry.holder if isinstance(entry.holder, dict) else {}
        if nonce and holder.get("nonce"):
            return str(holder["nonce"]) == nonce
        declared = str(holder.get("cwd") or "")
        if cwd and declared:
            return _is_within(cwd, declared)
        return False

    def remove_if_nonce(
        self, resource_id: str, *, expect_nonce: str, reason: str
    ) -> RemovalResult:
        """**期待する nonce と一致するときだけ**消す（compare-and-swap）。

        無条件の ``unlink`` は、読んだエントリと消すエントリが同じである保証を持たない。
        A と B が同じ幽霊を見て、A が「消して取る」に成功した直後に B が消すと、
        **B は A の生きた宣言を消して**自分も取得に成功する。掲示板が防ぐと宣言している
        二重取得そのものである。ロックで囲っても、ロックが外れた瞬間だけこの穴が開く。

        そこで削除を条件付きにする。手順は 3 段で、正しさは 2 段目の**原子性**に乗る。

        1. 安く先読みして、明らかに別物なら何も動かさずに諦める
        2. ``os.rename`` で一時名へ**捕まえる**。成功できるのは 1 人だけである
           （成功した瞬間に元の名前は消えるので、同時に走った他方は「無い」になる）
        3. 捕まえた中身の nonce を確かめ、一致すれば消す。違えば**元へ戻す**

        Returns
        -------
        RemovalResult
            消した / 無かった / 別物だった / 消せなかった。

        Notes
        -----
        **幽霊の退去は読み手の圧力で失敗しうる。** 2 段目の ``os.rename`` は、
        Windows では他プロセスが読んでいる最中のファイルに対して ``PermissionError``
        になる（Python の ``open()`` は ``FILE_SHARE_DELETE`` を付けないため）。
        フックが全セッションの全プロンプトで掲示板を読むので、これは例外的な事態ではない。
        数回やり直して吸収し、吸収できなければ ``FAILED`` を返して**保守的に諦める**
        （消せていないのに消えたと答えるより、退けられなかったと答えるほうが安全である）。
        """
        # 1. 先読み。ここで弾ければファイルを動かさずに済み、戻す処理も走らない。
        current = self.read(resource_id)
        if current is None:
            return RemovalResult.ABSENT
        if current.nonce != expect_nonce:
            self.audit("remove_refused", resource=resource_id, reason="nonce が一致しない")
            return RemovalResult.NOT_OWNED

        return self._capture_and_remove(
            self.path_for(resource_id),
            expect_nonce=expect_nonce,
            resource_id=resource_id,
            reason=reason,
        )

    def _capture_and_remove(
        self, path: Path, *, expect_nonce: str, resource_id: str, reason: str
    ) -> RemovalResult:
        """**捕まえてから確かめて消す**（CAS の 2〜3 段目）。先読みは呼び出し側の仕事。

        パスを引数に取るのは、主宣言（``board/<safe>.json``）と相乗り
        （``board/joins/<safe>.json``）で**同じ形を使う**ためである。無条件の ``unlink``
        は、読んだ内容と消す対象が同じである保証を持たない。読んでから消すまでの間に
        別プロセスが同じ名前を消して作り直すと、**新しい生きた申告を消す**。
        主宣言で塞いだ read-delete race と同じものなので、相乗りにも同じ形を適用する。

        Returns
        -------
        RemovalResult
            消した / 無かった / 別物だった / 消せなかった。
        """
        # 2. 捕まえる。ここだけが排他の根拠であり、ロックの有無に依存しない。
        tombstone = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.taken")
        moved, error = _rename_with_retry(path, tombstone)
        if moved is MoveResult.ABSENT:
            return RemovalResult.ABSENT
        if moved is not MoveResult.MOVED:
            self.audit("remove_failed", resource=resource_id, error=error)
            return RemovalResult.FAILED

        # 3. 捕まえた中身を確かめる。先読みとの間に入れ替わっていたら戻す。
        captured = _read_entry_at(tombstone)
        if captured is None or captured.nonce != expect_nonce:
            self._restore(tombstone, path, resource_id)
            self.audit("remove_refused", resource=resource_id, reason="捕まえた宣言が別物だった")
            return RemovalResult.NOT_OWNED

        result, error = _unlink_with_retry(tombstone)
        if result is RemovalResult.FAILED:
            # 掲示板からは既に消えている（名前を外した）。残骸は掲示板に載らない名前なので
            # 実害は無いが、消せなかった事実は残す。
            self.audit("tombstone_left", resource=resource_id, error=error)
        self.audit("removed", resource=resource_id, reason=reason)
        return RemovalResult.REMOVED

    def _restore(self, tombstone: Path, path: Path, resource_id: str) -> None:
        """捕まえたエントリを元の名前へ戻す。

        戻せるのは、その間に誰も新しい宣言を作っていないときだけである。``os.link``
        を使うのは、宛先があるときに**必ず失敗する**からである（``os.rename`` は
        POSIX では黙って上書きする）。既に新しい宣言があるなら、捕まえた側は
        既に退けられた古い宣言なので破棄してよい。

        Notes
        -----
        二重に失敗したときの結末を**事象名で分ける**。「宛先に新しい宣言がある」は
        捕まえた側が用済みというだけで害が無いが、「戻せなかった」は
        **他人の生きた宣言が掲示板から消え、資源が空きに見える**唯一の状態である。
        後者だけを ``declaration_lost`` という専用の名前で残す。この状態では、
        監査ログから grep 一発で見つかることが回復手段そのものだからである。
        """
        try:
            os.link(tombstone, path)
        except FileExistsError:
            self._drop_captured(tombstone, resource_id)
            return
        except OSError:
            # ハードリンクが使えないファイルシステム。上書きの危険を承知で戻す
            # （戻さなければ宣言が消えたままになり、そちらのほうが有害である）。
            moved, error = _rename_with_retry(tombstone, path)
            if moved is MoveResult.MOVED:
                return
            if moved is MoveResult.BLOCKED:
                # 宛先に既に新しい宣言がある。``FileExistsError`` 側と同じ結末なので、
                # tombstone の始末も同じ経路へ寄せる（片方だけ残す非対称を作らない）。
                self._drop_captured(tombstone, resource_id)
                return
            # 戻す先も戻す手段も無い。**宣言は掲示板から消えたままである。**
            captured = _read_entry_at(tombstone)
            self.audit(
                "declaration_lost",
                resource=resource_id,
                job=captured.job if captured is not None else None,
                tombstone=str(tombstone),
                path=str(path),
                error=error or str(moved),
            )
            return
        _unlink_with_retry(tombstone)

    def _drop_captured(self, tombstone: Path, resource_id: str) -> None:
        """捕まえたものを破棄する。既に新しい宣言があるときの後始末。

        捕まえたのは**既に退けられた古い宣言**なので、戻す先が無くても失うものは無い。
        残骸を消しておかないと、捕獲直後に死ななくても tombstone が溜まっていく。
        """
        self.audit("restore_dropped", resource=resource_id, reason="新しい宣言が既にある")
        _unlink_with_retry(tombstone)

    def remove_if_owned(
        self, resource_id: str, *, reason: str, nonce: str | None = None, cwd: str | None = None
    ) -> RemovalResult:
        """自分の宣言であるときだけ削除する。

        「読んで、所有を確かめて、消す」までが 1 つの操作である。nonce を持つ宣言は
        :meth:`remove_if_nonce` の CAS で消すため、読んでから消すまでに他セッションが
        ``claim --force`` で取り直しても**新しい宣言を消さない**。ロックは競り合いを
        減らすために掛けるだけで、取れなくても正しさは変わらない。

        ロックが取れないときは**囲わずに続行する**。解放できずに宣言を残すほうが有害
        （幽霊が資源を占有し続ける）であり、CAS という主防御は失われないためである。
        ロック無しで消したことは監査ログに残す。

        Returns
        -------
        RemovalResult
            消した / 無かった / 他人のものだった / 消せなかった。呼び出し側が
            **事実と違う説明**を出さないよう、4 つを畳まずに返す。
        """
        with self.locked(resource_id) as lock:
            if lock is not LockState.ACQUIRED:
                self.audit("remove_unlocked", resource=resource_id, lock=str(lock))
            entry = self.read(resource_id)
            if entry is None:
                return RemovalResult.ABSENT
            if not self.owns(entry, nonce=nonce, cwd=cwd):
                self.audit("remove_refused", resource=resource_id, reason="他者の宣言のため")
                return RemovalResult.NOT_OWNED
            if entry.nonce:
                return self.remove_if_nonce(resource_id, expect_nonce=entry.nonce, reason=reason)
            # nonce を持たない古いエントリ。照合できる値が無いので従来どおり消す。
            return self.remove_detailed(resource_id, reason=reason)

    def list_all(self) -> list[Entry]:
        """全エントリを読む。読めなかったものは黙って飛ばす。"""
        try:
            paths = sorted(self.entries_dir.glob("*.json"))
        except OSError:
            return []

        entries: list[Entry] = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self.audit("entry_unreadable", path=str(path), error=str(exc))
                continue
            entry = Entry.from_dict(data)
            if entry is not None:
                entries.append(entry)
        return entries

    def try_claim(self, entry: Entry) -> bool:
        """エントリを作成して資源を宣言する。既にあれば False。

        ``O_CREAT | O_EXCL`` による作成なので、複数セッションが同時に宣言しても
        成功するのは 1 つだけである。

        Notes
        -----
        既存エントリが幽霊かどうかの判断は**ここでは行わない**。判定は
        :mod:`resource_broker.liveness` の責務で、幽霊を退かしてから
        取り直すかどうかは呼び出し側（CLI）が決める。
        """
        path = self.path_for(entry.resource)
        try:
            self.entries_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.audit("mkdir_failed", resource=entry.resource, error=str(exc))
            return False

        payload = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if not self._create_exclusively(path, payload, entry.resource):
            return False

        # 見積もりも残す。次に同じ資源を使うとき、前回どう見積もったかを振り返れる
        # ようにするためである（`rb history`）。精度は回ごとに上げていくしかない。
        self.audit(
            "claimed",
            resource=entry.resource,
            job=entry.job,
            pid=entry.pid,
            eta=entry.eta,
            usage=entry.usage,
            sharing=entry.sharing or None,
        )
        return True

    def _create_exclusively(
        self, path: Path, payload: str, resource_id: str, *, kind: str = "claim"
    ) -> bool:
        """中身の入ったファイルを**先着 1 名で**作る。作れたら True。

        ``O_EXCL`` で作ってから書くと、作成と書き込みの間に読んだ他セッションが
        **空のファイル**を見る。取得の排他自体は崩れない（作成が先着 1 名だから）が、
        フックや ``rb status`` から宣言が一瞬消える。先に一時ファイルへ書いてから
        ハードリンクで名前を付ければ、見えた瞬間には中身が揃っている。
        ハードリンクが使えないファイルシステムでは従来の手順へ退避する。
        """
        temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
        try:
            temporary.write_text(payload, encoding="utf-8")
        except OSError as exc:
            self.audit(f"{kind}_write_failed", resource=resource_id, error=str(exc))
            return False

        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            return self._create_by_exclusive_open(path, payload, resource_id, kind=kind)
        finally:
            _unlink_with_retry(temporary)

    def _create_by_exclusive_open(
        self, path: Path, payload: str, resource_id: str, *, kind: str = "claim"
    ) -> bool:
        """``O_EXCL`` で作ってから書く。ハードリンクが使えない環境の退避路。"""
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            self.audit(f"{kind}_failed", resource=resource_id, error=str(exc))
            return False

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
        except OSError as exc:
            self.audit(f"{kind}_write_failed", resource=resource_id, error=str(exc))
            _unlink_with_retry(path)
            return False
        return True

    @property
    def joins_dir(self) -> Path:
        """相乗りの申告を置くディレクトリ。

        主宣言（``board/*.json``）とは**別の場所**に置く。同じ場所に混ぜると
        ``list_all`` が相乗りを主宣言として拾い、資源が二重に載って見える。
        """
        return self.entries_dir / "joins"

    def join_path(self, resource_id: str, cwd: str) -> Path:
        """相乗り申告のパス。作業ディレクトリごとに 1 つ。"""
        key = naming.safe_filename(f"{resource_id}|{os.path.normcase(cwd)}")
        return self.joins_dir / f"{key}.json"

    def add_join(self, entry: Entry, cwd: str) -> bool:
        """相乗りを申告する。**残せたときだけ** True。

        「既にある」と「残せなかった」を区別する必要があるときは
        :meth:`add_join_detailed` を使う。
        """
        return self.add_join_detailed(entry, cwd) is JoinResult.ADDED

    def add_join_detailed(self, entry: Entry, cwd: str) -> JoinResult:
        """相乗りを申告し、結果を 3 値で返す。同じ作業ディレクトリから二重には申告できない。

        Returns
        -------
        JoinResult
            追加した / 既にある / 掲示板に残せなかった。**畳まない**
            （:class:`JoinResult` 参照）。

        Notes
        -----
        **相乗りしてよいかは判定しない。** 保持者が ``sharing`` に何を書いていようと、
        本ツールは可否を決めない。可否は当事者の合意事項であり、ここでやるのは
        「入ったことを見えるようにする」ことだけである。黙って入られるより遥かによい。
        """
        path = self.join_path(entry.resource, cwd)
        try:
            self.joins_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.audit("join_mkdir_failed", resource=entry.resource, error=str(exc))
            return JoinResult.FAILED

        payload = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if not self._create_exclusively(path, payload, entry.resource, kind="join"):
            # 作れなかった理由を分ける。**ファイルがあるかどうか**でしか区別できないが、
            # 「既に自分（誰か）の申告がある」と「書けなかった」は対処がまるで違う。
            if path.exists():
                return JoinResult.EXISTS
            self.audit("join_failed", resource=entry.resource, cwd=cwd)
            return JoinResult.FAILED

        self.audit("joined", resource=entry.resource, job=entry.job, cwd=cwd)
        return JoinResult.ADDED

    def _load_joins(self) -> list[tuple[Path, Entry]]:
        """相乗り申告をパスつきで読む。読めなかったものは飛ばす。

        **確定的な幽霊だけをここで捨てる。** 相乗りには主宣言と違って幽霊を退ける経路が
        無い（``claim`` に相当する操作が無い）ため、落ちたセッションの申告が永久に残り、
        その資源を「使用中」に固定してしまう。``rb wait`` は二度と RELEASED を返さず、
        フックは全セッションの全プロンプトに出し続ける。

        捨てるのは ``since < 現在の起動時刻`` の 1 つだけとする。再起動で全 PID が無効に
        なるため推測を含まない。**猶予や PID を使った推測はしない**（実測が「空き」でも
        宣言が幽霊である証明にはならないという非対称性を崩さないため。
        CLAUDE.md「Liveness Judgment」）。

        **捨てるときも無条件では消さない。** 読んでから消すまでの間に、別セッションが
        同じ ``(資源, cwd)`` の申告を取り下げて出し直すことがある。無条件の ``unlink``
        はその**新しい生きた申告**を消す。主宣言で塞いだ read-delete race と同じものなので、
        同じ捕獲型 CAS（:meth:`_capture_and_remove`）に載せる。
        """
        try:
            paths = sorted(self.joins_dir.glob("*.json"))
        except OSError:
            return []

        boot = platform_info.boot_time()
        # **境界に余裕を持たせる。** boot は起動からの経過時間から逆算した値なので、
        # NTP が時計を前方へ飛ばすと、直前に出したばかりの相乗りが起動より前に見える。
        # 1 分引いておけばその窓がゼロになる。失うのは再起動直後の掃除の遅れだけである。
        cutoff = boot - timedelta(seconds=JOIN_BOOT_MARGIN_S) if boot is not None else None
        loaded: list[tuple[Path, Entry]] = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            entry = Entry.from_dict(data)
            if entry is None:
                continue
            since = entry.since_dt
            if cutoff is not None and since is not None and since < cutoff:
                removed = self._capture_and_remove(
                    path,
                    expect_nonce=entry.nonce,
                    resource_id=entry.resource,
                    reason="再起動をまたいだ相乗り",
                )
                if removed is RemovalResult.REMOVED:
                    self.audit("join_stale_removed", resource=entry.resource, since=entry.since)
                    continue
                if removed is RemovalResult.ABSENT:
                    continue  # 読んだ直後に誰かが消した。もう無い
                # 消せなかった、または捕まえたら別物だった（新しい申告が出ている）。
                # **消せていないものを「無い」と答えない。** 資源が空きに見えて
                # 他セッションが取りにくる。読めた事実のほうを残す。
                loaded.append((path, entry))
                continue
            loaded.append((path, entry))
        return loaded

    def list_joins(self, resource_id: str) -> list[Entry]:
        """その資源への相乗り申告を読む。読めなかったものは飛ばす。"""
        return [entry for _, entry in self._load_joins() if entry.resource == resource_id]

    def list_all_joins(self) -> list[Entry]:
        """全ての相乗り申告を読む。資源の一覧を作るときに使う。"""
        return [entry for _, entry in self._load_joins()]

    def remove_join(self, resource_id: str, cwd: str, *, reason: str) -> bool:
        """指定した作業ディレクトリの相乗り申告を取り下げる（パスの完全一致）。"""
        result, error = _unlink_with_retry(self.join_path(resource_id, cwd))
        if result is RemovalResult.FAILED:
            self.audit("join_remove_failed", resource=resource_id, error=error)
        if result is not RemovalResult.REMOVED:
            return False
        self.audit("join_removed", resource=resource_id, cwd=cwd, reason=reason)
        return True

    def find_own_join(self, resource_id: str, cwd: str) -> tuple[Path, Entry] | None:
        """自分の相乗り申告を**探すだけ**。消さない。

        照合は主宣言（:meth:`owns`）と**同じ規則**にする。パスの完全一致だけにすると、
        申告したときと違うディレクトリから ``release`` したときに
        ``join_path`` のキーが変わって自分の申告を外せない。主宣言は祖先関係を
        許すのに相乗りだけ完全一致、という非対称は使う側から見て説明できない。

        ただし**完全一致を先に試す**。祖先関係だけで選ぶと、宣言者の cwd が自分の
        祖先でありさえすればどれでも一致するため、ハブのルートから出された**他人の**
        相乗りを配下の全セッションが外せてしまう。祖先候補が複数あるときは
        ``declared_cwd`` が最長（＝最も自分に近い）ものを選ぶ。同じ理由で、
        自分がまさにその場所から出した申告があるなら、それ以外を選ぶ理由は無い。

        探索と削除を分けてあるのは、呼び出し側が**消す前に候補の有無を知る**必要が
        あるためである（``rb release`` は主宣言と相乗りの両方が候補になるとき、
        どちらも消さずに指定を求める。DESIGN.md「Ownership」）。

        Returns
        -------
        tuple of (Path, Entry) or None
            選んだ申告のパスと中身。候補が無ければ None。
        """
        exact = self.join_path(resource_id, cwd)
        candidates = [
            (path, entry)
            for path, entry in self._load_joins()
            if entry.resource == resource_id and self.owns(entry, cwd=cwd)
        ]
        for path, entry in candidates:
            if path == exact:
                return (path, entry)
        if candidates:
            # 最も近い祖先を選ぶ。declared_cwd が長いほど自分に近い。
            return max(candidates, key=lambda item: len(str(item[1].holder.get("cwd") or "")))
        return None

    def remove_own_join(self, resource_id: str, cwd: str, *, reason: str) -> Entry | None:
        """自分の相乗り申告を取り下げる。選び方は :meth:`find_own_join` と同じ。

        Returns
        -------
        Entry or None
            取り下げた申告。外せるものが無ければ None。**bool ではなく申告そのものを返す**
            のは、呼び出し側が「どの場所から出された申告を消したか」を表示できるように
            するためである。祖先フォールバックで他人の申告を消したとき、誤爆が
            目視できなければ気づく手段が無い。
        """
        chosen = self.find_own_join(resource_id, cwd)
        if chosen is None:
            return None

        path, entry = chosen
        result, error = _unlink_with_retry(path)
        if result is RemovalResult.FAILED:
            self.audit("join_remove_failed", resource=resource_id, error=error)
            return None
        if result is RemovalResult.REMOVED:
            self.audit("join_removed", resource=resource_id, cwd=cwd, reason=reason)
            return entry
        return None

    def remove_joins(self, resource_id: str, *, reason: str) -> int:
        """その資源の相乗り申告を**全て**取り下げる。消せた件数を返す。

        ``release --force`` から使う。主宣言だけ強制解放できても、残った相乗りが
        資源を「使用中」に固定し続けるため、掃除する手段が無いことになる。
        """
        removed = 0
        for path, entry in self._load_joins():
            if entry.resource != resource_id:
                continue
            result, error = _unlink_with_retry(path)
            if result is RemovalResult.REMOVED:
                removed += 1
                self.audit("join_removed", resource=resource_id, reason=reason)
            elif result is RemovalResult.FAILED:
                self.audit("join_remove_failed", resource=resource_id, error=error)
        return removed

    def replace(
        self, entry: Entry, *, reason: str, expect_nonce: str | None = None
    ) -> UpdateResult:
        """既存のエントリを差し替える。取得ではなく**更新**である。

        ``try_claim`` と違い ``O_EXCL`` は使わない。ここは資源の取得ではなく、
        既に持っている宣言の書き換えだからである。取り違えないよう、
        **資源の取得は ``try_claim`` だけ**が担う。

        Parameters
        ----------
        expect_nonce : str, optional
            期待する現在の宣言の nonce。読んでから書くまでの間に保持者が入れ替わっていた場合、
            **古い内容で新しい宣言を潰さない**ための照合である。渡さないと無条件上書きになる。

            ``since`` では照合しない。秒精度なので、同じ秒に解放と再取得が起きると
            別の宣言を同じものと誤認する（テストで実際に踏んだ）。

        Returns
        -------
        UpdateResult
            置いた / nonce が一致しなかった / 書けなかった。**競合と I/O 失敗を畳まない。**
            畳むと共有違反にまで「宣言が変わった可能性」という説明が付く。

        Notes
        -----
        削除（:meth:`remove_if_nonce`）と違い、ここでは「捕まえてから確かめる」形を採らない。
        捕まえた直後にプロセスが死ぬと**宣言そのものが消える**からである。宣言が消えれば
        資源は空きに見え、他セッションが取りにくる。更新が守るのは自分の申告値であって
        所有権の移動ではないため、失うものの大きさが釣り合わない。
        したがってここは「読んで確かめてから原子的に置く」に留め、読みと置きの間の
        極小の窓は既知の残余として受け入れる。

        その窓には**「蘇生」の向きもある**。``os.replace`` は宛先が無ければ作るため、
        読んでから書くまでの間に宣言が**正当に消えていた**場合（当人の ``release`` や
        ``rb run`` の後始末）、ここが**死んだ宣言を書き戻す**。蘇生した宣言は
        nonce の持ち主が既にいないので、``rb release --force`` でしか消せない。
        照合が防ぐのは「古い内容で新しい宣言を潰す」向きだけであり、この向きは防げない。
        """
        if expect_nonce is not None:
            current = self.read(entry.resource)
            if current is None or current.nonce != expect_nonce:
                self.audit("update_conflict", resource=entry.resource, expected=expect_nonce)
                return UpdateResult.CONFLICT

        path = self.path_for(entry.resource)
        try:
            self.entries_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.audit("update_mkdir_failed", resource=entry.resource, error=str(exc))
            return UpdateResult.FAILED

        # 書き込み中に他セッションが読んでも壊れた JSON を見ないよう、一時ファイル経由で置換する。
        payload = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            self.audit("update_write_failed", resource=entry.resource, error=str(exc))
            _unlink_with_retry(temporary)
            return UpdateResult.FAILED

        replaced, error = _replace_with_retry(temporary, path)
        if not replaced:
            self.audit("update_failed", resource=entry.resource, error=error)
            _unlink_with_retry(temporary)
            return UpdateResult.FAILED

        self.audit("updated", resource=entry.resource, reason=reason)
        return UpdateResult.REPLACED

    def remove(self, resource_id: str, *, reason: str) -> bool:
        """エントリを削除する。消せたときだけ True。理由は監査ログに残す。

        「無かった」と「消せなかった」を区別する必要があるときは
        :meth:`remove_detailed` を使う。
        """
        return self.remove_detailed(resource_id, reason=reason) is RemovalResult.REMOVED

    def remove_detailed(self, resource_id: str, *, reason: str) -> RemovalResult:
        """エントリを削除し、結果を 3 値で返す。

        Windows では他プロセスが読んでいる最中の削除が共有違反になる。フックが
        全セッションの全プロンプトで掲示板を読むため、「消せなかった」は実際に起こる。
        「無かった」と畳むと、呼び出し側が嘘の説明を出すことになる。
        """
        result, error = _unlink_with_retry(self.path_for(resource_id))
        if result is RemovalResult.REMOVED:
            self.audit("removed", resource=resource_id, reason=reason)
        elif result is RemovalResult.FAILED:
            self.audit("remove_failed", resource=resource_id, error=error)
        return result

    def audit(self, event: str, **fields: object) -> None:
        """監査ログに 1 行追記する。失敗しても黙って諦める。"""
        audit.append(self.root, event, **fields)


def build_entry(
    resource_id: str,
    *,
    job: str,
    display: str = "",
    log: str | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    session: str | None = None,
    observed: dict[str, object] | None = None,
    eta: str = "",
    peak: str = "",
    avg: str = "",
    sharing: str = "",
) -> Entry:
    """宣言用の Entry を組み立てる。時刻と boot はここで機械生成する。

    呼び出し側に時刻を渡させない。LLM が時刻を書く経路を作らないための設計である。

    Notes
    -----
    ``pid`` は**既定で記録しない**。手動の ``claim`` を実行するのは CLI プロセスであり、
    それは即座に終了する。その PID を宣言者として記録すると「宣言プロセスが死んでいる」
    と判定され、ジョブが資源を掴む前に幽霊扱いされてしまう。
    PID を記録するのはラッパー（Phase 2 の ``rb run``）だけで、そこでは
    ラッパー自身がジョブと同じ寿命を持つため生存確認が意味を持つ。
    """
    boot = platform_info.boot_time()
    return Entry(
        resource=resource_id,
        display=display or naming.display_default(resource_id),
        holder={
            "session": session or Path(cwd or os.getcwd()).name,
            "cwd": cwd or os.getcwd(),
            "pid": pid,
            "job": job,
            # 宣言ごとに一意。所有者の照合に使う。cwd だけでは、同じ場所から
            # 解放と再取得が起きたときに「別の宣言」を自分のものと誤認する
            "nonce": uuid.uuid4().hex,
        },
        log=log,
        since=clock.now_iso(),
        boot=clock.to_iso(boot) if boot else None,
        observed=_stamp_observed(observed),
        eta=build_eta(eta),
        usage={"peak": peak, "avg": avg} if (peak or avg) else None,
        sharing=sharing,
    )


def build_eta(text: str) -> dict[str, object] | None:
    """ETA の申告を組み立てる。**時刻の計算は機械が行う。**

    申告するのはセッション（LLM）だが、「30 分後は何時か」を LLM に書かせない。
    JST と UTC の取り違えや単純な足し算の誤りが実際に起きているためである
    （CLAUDE.md「Time Handling」）。``30m`` のような期間表記が読めたときだけ
    絶対時刻を機械が付ける。読めなければ申告文だけを残す。

    Notes
    -----
    **ETA は判断に使わない。** 掲示板が持つのは観測点であり、期限を過ぎたからといって
    宣言を退けたり、待機を打ち切ったりはしない。人間とセッションが読むための材料である。
    """
    if not text:
        return None
    duration = clock.parse_duration(text)
    return {
        "stated": text,
        "at": clock.to_iso(clock.now() + duration) if duration else None,
    }


def _stamp_observed(observed: dict[str, object] | None) -> dict[str, object] | None:
    """実測に観測時刻を刻む。

    「いつ観測したか」が無いと、掲示板に残った実測値がどの時点のものか分からない。
    時刻はここで機械生成する（DESIGN.md「Board Schema」の ``observed.at``）。
    """
    if observed is None:
        return None
    return {"at": clock.now_iso(), **observed}
