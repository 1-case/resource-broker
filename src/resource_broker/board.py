"""掲示板の読み書き。

掲示板は**宣言ごとに 1 ファイル**の JSON で、名前は nonce である。単一ファイルに
集約しないのは、破損の被害を 1 件に閉じ込めるためである（DESIGN.md「Architecture」）。
**ファイル名は身元を持たない**——名前から身元を導くと、鍵の構成を変えた瞬間に判定が
黙って恒偽になる。

**削除の正しさは nonce の compare-and-swap（CAS）が担保する。** 「期待する nonce と
一致するときだけ消す／置く」の形にしてあり、ロックが取れても取れなくても
「**読んだ宣言以外を消さない**」は変わらない。

**取得の直列化はロックだけが担う。** 名前が nonce なので ``O_EXCL`` は取得競合を
解決しない（衝突しないので必ず作れる）。つまり取得については、このモジュールが他の
全箇所で避けてきた「ロックが外れた瞬間だけ排他が消える」という相関を**受け入れて
いる**。Windows では競合が激しいときほど ``PermissionError`` が起きやすく、この相関は
現実に起こる。**取れなかったことは呼び出し側が必ず告げる**（DESIGN.md「Per-Resource
Lock」）。条件付き書き込みのプリミティブがファイルシステムに無いため、平坦な宣言の
集まりに対してこれ以上の保証は置けない。

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
from datetime import datetime
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
#:
#: **既知のキーが想定外の型で来た場合も落とさない。** ``from_dict`` は型が合わなければ
#: 既定値へ倒すが、そのとき元の値を ``extra`` へ退避する。退避しないと、スキーマを
#: 拡張した新しい版が書いた値を、古い版が読んで書き戻した瞬間に**黙って消す**。
#: 前方互換は「未知のキー」だけでなく「既知のキーの未知の形」も守る必要がある。
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


def _json_files(directory: Path) -> tuple[list[Path], bool]:
    """ディレクトリ内の ``*.json`` を並べる。**「読めない」を「空」と混ぜない。**

    ``Path.glob`` を使ってはならない。``glob`` は ``OSError`` を内部で握り潰して
    空を返すため、次のいずれも**空の掲示板と同じ形**で返ってくる。

    - 掲示板のディレクトリが通常ファイルになっている
    - 権限で拒否されている（ACL、別ユーザーの所有）
    - 切断されたネットワークパスを ``RESOURCE_BROKER_HOME`` に指している

    「空きだ」と断定して全セッションへ配るのは、このツールが最もやってはならない
    ことである（DESIGN.md「Liveness Judgment」の非対称性の裏返し）。``os.scandir``
    は ``NotADirectoryError`` / ``PermissionError`` をそのまま投げるので区別できる。

    Returns
    -------
    tuple of (list of Path, bool)
        読めたファイルと、**読めなかったものがあったか**。
    """
    found: list[Path] = []
    unreadable = False
    try:
        for entry in os.scandir(directory):
            if not entry.name.endswith(".json"):
                continue
            if entry.is_file():
                found.append(Path(entry.path))
                continue
            # **壊れたリンクを黙って落とさない。** ``is_file()`` は
            # ``FileNotFoundError`` を内部で False に畳むので、リンク先を失った
            # 宣言が「そもそも無かった」と同じ形になる。
            if entry.is_symlink():
                unreadable = True
    except FileNotFoundError:
        return [], False  # まだ誰も宣言していない。これは「空」であって「読めない」ではない
    except OSError:
        return [], True
    return sorted(found), unreadable


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
    #: 読んだときのスキーマ版。**書き戻しても下げない。**
    #: 新しい版が書いた未知のフィールドは ``extra`` が保つのに、それが新スキーマ由来だと
    #: いう印だけを消すと、次に読む側は「古い形のエントリに知らない鍵が混じっている」と
    #: 見ることになる。保った値は前方互換の最後の 1 マスである。
    schema: int = SCHEMA

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
                "schema": self.schema,
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
            extra=cls._salvage(data),
            schema=cls._read_schema(data),
        )

    @staticmethod
    def _read_schema(data: dict[str, object]) -> int:
        """読んだスキーマ版。壊れていれば現行版とみなす。

        ``bool`` は ``int`` の派生なので明示的に除く。
        """
        value = data.get("schema")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return SCHEMA
        return value

    @staticmethod
    def _salvage(data: dict[str, object]) -> dict[str, object]:
        """書き戻しても失われない形で、拾えなかった値を退避する。

        **前方互換は「未知のキー」だけでは足りない。** 既知のキーが想定外の型で来ると
        上の分岐が既定値へ倒し、``extra`` にも入らないため、**読んで書き戻した瞬間に
        黙って消える**。スキーマを拡張した新しい版が書いた値を、古い版が消す形になる。

        型が合わなかった既知のキーは ``x-<キー名>`` として退避する。名前を変えるのは、
        書き戻したときに壊れた値が正規の位置へ戻らないようにするためである。
        """
        expected: dict[str, type | tuple[type, ...]] = {
            # ``schema`` もここに入れる。**形式を識別する当のキーを例外にしない。**
            # 将来 semver 文字列（``"2.1"``）や dict を採る版が現れたとき、旧版が読んで
            # 書き戻すと版の印だけが現行版に化け、拡張フィールドは ``extra`` に残る
            # ——つまり「schema 1 なのに未来の鍵がある」という、この仕組みが防ごうと
            # している状態そのものになる。
            "schema": int,
            "display": str,
            "holder": dict,
            "log": str,
            "since": str,
            "boot": str,
            "observed": dict,
            "eta": dict,
            "usage": dict,
            "sharing": str,
        }
        extra: dict[str, object] = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}
        for key, kind in expected.items():
            # **``_read_schema`` が拒む条件とそろえる。** そちらが「壊れている」と見た値を
            # ここが「型は合っている」と見ると、既定値へ倒れた元の値が退避されずに消える。
            # ``bool`` は ``int`` の派生なので明示的に除く（``kind`` が tuple でも効くよう
            # ``int in kinds`` の形で見る）。
            kinds = kind if isinstance(kind, tuple) else (kind,)
            value = data.get(key)
            broken = int in kinds and isinstance(value, bool)
            if key == "schema" and isinstance(value, int) and not isinstance(value, bool):
                broken = broken or value < 1
            if (
                key in data
                and data[key] is not None
                and (broken or not isinstance(data[key], kind))
            ):
                # **既にある ``x-`` を潰さない。** ``x-`` は拡張フィールドの慣例接頭辞で、
                # 別の版がその名前を使っている可能性がある。退避のために別の値を消せば、
                # この関数が防ごうとしている取りこぼしを自分で起こす。
                extra.setdefault(f"x-{key}", data[key])
        return extra


@dataclass(frozen=True)
class OwnRemoval:
    """自分の宣言を消した結果。**3 つを畳まない。**

    「1 件も消えなかった」には理由が 3 通りあり、対処がまるで違う——そもそも宣言が
    無かった / 全部他人のものだった / 消そうとして失敗した（Windows の共有違反）。
    畳むと呼び出し側が**事実と違う説明**を出す。
    """

    removed: list[Entry]
    """消せたもの。"""

    failed: list[Entry]
    """自分のものだが消せなかったもの（共有違反など）。**残っている**。"""

    swapped: list[Entry]
    """読んでから消すまでに**別の宣言へ入れ替わっていた**もの。

    ``failed`` と畳まない。I/O の失敗は再試行の話だが、入れ替わりは**他セッションが
    その資源を取り直した**という話で、次にやることがまるで違う。
    """

    foreign: list[Entry]
    """自分のものではないので触らなかったもの。"""

    @property
    def any_here(self) -> bool:
        """その資源に宣言が 1 件でもあったか。"""
        return bool(self.removed or self.failed or self.swapped or self.foreign)


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

    def lock_path(self, resource_id: str) -> Path:
        """取得の排他区間を守るロックのパス。"""
        return self.entries_dir / f"{naming.safe_filename(resource_id)}.lock"

    @contextmanager
    def locked(self, resource_id: str, *, wait_s: float = LOCK_WAIT_S) -> Iterator[LockState]:
        """書き換えの区間を囲う。**取得の排他はここに乗っている。**

        宣言のファイル名は nonce なので、``O_EXCL`` は**取得の競合を解決しない**
        （名前が衝突しないので必ず作れる）。「読んで、幽霊なら退けて、作る」を
        直列化するのはこの区間だけである。実測: この区間を外して読み書きの窓を
        開くと、12 プロセスが同一資源へ**全件**宣言できる。

        **削除の正しさは別に立っている。** 「読んだ宣言以外を消さない」は nonce の
        CAS（:meth:`remove_if_nonce`）が守っており、ロックの有無に依存しない。

        **取れなければ排他は無い。** その場合でも通す——ロックが取れないのは本ツール側の
        事情であって「資源が使用中だと確認できた」ではなく、そこで止めるのは fail-open に
        反する。**保証の範囲はロックが取れる間だけ**であり、取れなかったことは呼び出し側が
        必ず告げる。ロックを主防御に据える代償として、Windows で
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

    def declarations(self) -> list[tuple[Path, Entry]]:
        """掲示板にある**全ての宣言**を読む。読めなかったものは飛ばす。

        **主宣言と相乗りを区別しない。** 資源を使っている作業が N 件あるだけであり、
        どれが先かは ``since`` に書いてある（導出できるものを記録しない）。区別を
        持っていた頃は、片方が消えるともう片方の意味が変わる——主宣言が消えると
        走っている相乗りがいても「空き」になり、二重取得が成立した——という非対称が
        あった。対等にすれば、**どれがいつ消えても他の宣言の意味は変わらない。**

        旧い置き場（``board/<資源>.json`` と ``board/joins/*.json``）も**同じ宣言として
        読む**。稼働中のセッションの宣言を、形式を変えた瞬間に見失わないためである。
        """
        return self.declarations_detailed()[0]

    def declarations_detailed(self) -> tuple[list[tuple[Path, Entry]], bool]:
        """全ての宣言と、**読めなかったものがあったか**を返す。"""
        found: list[tuple[Path, Entry]] = []
        paths, unreadable = _json_files(self.entries_dir)
        legacy, legacy_unreadable = _json_files(self.entries_dir / "joins")
        for path in sorted(paths) + sorted(legacy):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                # 読めないのは「壊れている」とは別の事実である。**空だと言わない側。**
                self.audit("entry_unreadable", path=str(path), error=str(exc))
                unreadable = True
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                self.audit("entry_corrupt", path=str(path), error=str(exc))
                continue
            entry = Entry.from_dict(data)
            if entry is not None:
                found.append((path, entry))
        return found, unreadable or legacy_unreadable

    def unreadable_paths(self) -> list[Path]:
        """**どの資源にも紐づけられないファイル**を返す。

        壊れていて ``resource`` が読めない宣言は、資源を指して消すことができない。
        平坦化する前は ``board/<資源>.json`` という名前だったので名指しで消せたが、
        ファイル名が身元を持たなくなった以上、**中身が読めなければ持ち主も分からない**。

        塞ぐものは無い（宣言のファイル名は nonce なので、壊れたファイルがあっても
        新しい宣言は作れる）。残るのは掃除の手段だけなので、**場所を教える**。
        """
        found: list[Path] = []
        for directory in (self.entries_dir, self.entries_dir / "joins"):
            paths, _ = _json_files(directory)
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    # **「読めない」を「壊れている」と混ぜない。** 他セッションが捕獲の
                    # 途中で名前を外した瞬間（`FileNotFoundError`）や、一時的な共有違反
                    # （`PermissionError`）は、**生きた宣言**でも起こる。ここへ入れると
                    # `--clean` がそれを消し、掲示板は空・資源は掴まれたままになる。
                    continue
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    found.append(path)
                    continue
                if Entry.from_dict(data) is None:
                    found.append(path)
        return sorted(found)

    def remove_unreadable(self, *, reason: str) -> list[Path]:
        """読めないファイルを消す。消せたものを返す。

        資源を指定しない。**指定できない**——中身が読めないので、どの資源のものか
        分からない。``rb release --force`` が資源ごとに動くのと対照的だが、
        それは対象が「誰のものでもないゴミ」だからである。
        """
        removed: list[Path] = []
        for path in self.unreadable_paths():
            # **消す直前にもう一度確かめる。** 一覧を作ってから消すまでの間に、正常な
            # 宣言がそこへ現れうる（旧い置き場は資源ごとの固定パスである）。
            if path not in set(self.unreadable_paths()):
                continue
            result, error = _unlink_with_retry(path)
            if result is RemovalResult.REMOVED:
                removed.append(path)
                self.audit("unreadable_removed", path=str(path), reason=reason)
            elif result is RemovalResult.FAILED:
                self.audit("remove_failed", path=str(path), error=error)
        return removed

    def list_for(self, resource_id: str) -> list[Entry]:
        """その資源の宣言を**古い順**に返す。

        順序は ``since`` で決まる。「どれが先に取ったか」を別に記録しない——
        時間的に後のものが後から来たに決まっている。
        """
        return [entry for _, entry in self.pairs_for(resource_id)]

    def pairs_for(self, resource_id: str) -> list[tuple[Path, Entry]]:
        """その資源の宣言を、パスつきで古い順に返す。"""
        found = [(path, e) for path, e in self.declarations() if e.resource == resource_id]
        return sorted(found, key=lambda item: (item[1].since, str(item[0])))

    def declaration_path(self, nonce: str) -> Path:
        """宣言 1 件のパス。**ファイル名は nonce だけで、身元を持たない。**

        資源 ID や cwd をファイル名へ入れない。入れると「名前が一致するか」で
        身元を判定したくなり、鍵の構成を変えた瞬間に判定が黙って恒偽になる
        （実際に一度そうなり、走行中の宣言を解放する経路になった）。
        """
        return self.entries_dir / f"{naming.safe_filename(nonce)}.json"

    def owns(
        self,
        entry: Entry,
        *,
        nonce: str | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """そのエントリが自分のものか。**材料は 3 段階で、強い順に使う。**

        1. **nonce**: 一致すれば確実に自分のものである（宣言ごとに一意）。持っているのは
           ラッパー（``rb run``）だけで、自分が作った宣言を自分で消す場面に限る
        2. **session_id**: 両者が持っていれば、それだけで決める。**cwd は見ない。**
           同じリポジトリで 2 つのセッションを立てるのは日常であり、そのとき cwd も
           ``session``（cwd のベース名）も一致するため、**cwd では互いを区別できない**。
           区別しないと、**自分の宣言を消したつもりで他セッションの宣言を消す**
        3. **cwd**: どちらも無いときの従来の照合。**宣言者の場所が自分の場所と同じか、
           自分の祖先のとき**だけ自分のものとみなす。宣言したときと解放するときで作業
           ディレクトリが違うことは普通にある（サブディレクトリへ降りた等）ので、そこで
           弾くと自分の資源を自分で解放できない。逆方向（自分が宣言者の祖先）は**認めない**。
           このマシンでは全プロジェクトが 1 つのルートの下にあるため、認めるとハブのルートで
           動くセッションが全アセットの宣言を ``--force`` 無しで解放・更新できてしまう

        **session_id が無いことを理由に厳しくしない。** 片方でも欠けていれば cwd へ落とす。
        古い宣言（session_id を持たない）や Claude Code 以外からの利用を締め出さないためである
        （CLAUDE.md「Fail-Open」）。

        **所有者を確かめずに消してはならない。** 他セッションの生きた宣言を消すと、
        掲示板は空・資源は掴まれたままという最も検出しにくい不整合ができる。
        """
        holder = entry.holder if isinstance(entry.holder, dict) else {}
        if nonce:
            # **呼び出し側が nonce を持っているなら、そこで決める。** 相手に nonce が
            # 無いことを「照合できないので次へ」と読むと、自分の宣言が外部から
            # 強制解放された後に同じ場所へ出た**別人の古い形式の宣言**を、後始末が
            # 自分のものとして消す。nonce を持たない操作（手動の release）だけが
            # 下のフォールバックへ落ちる。
            return str(holder.get("nonce") or "") == nonce
        declared_session = str(holder.get("session_id") or "")
        if declared_session and session_id:
            return declared_session == session_id
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
        # 1. 先読み。**中身で探す。** ファイル名から場所を組み立てない——名前の付け方を
        #    変えた瞬間に「見つからない」へ黙って倒れる（実際に一度そうなった）。
        if not expect_nonce:
            # **空文字を鍵にしてはならない。** nonce を持たない古い宣言は複数ありうるので、
            # 「nonce が空のもの」に一致させると**別の生きた宣言**を捕まえて消す。
            # 古い宣言の削除は :meth:`remove_matching` が中身の照合で行う。
            self.audit("remove_refused", resource=resource_id, reason="nonce が空である")
            return RemovalResult.NOT_OWNED

        pairs = self.pairs_for(resource_id)
        found = [(path, e) for path, e in pairs if e.nonce == expect_nonce]
        if not found:
            # **「無い」と「別物になっている」を畳まない。** 宣言が残っているのに
            # nonce が違うのは、解放と再取得が挟まったということで、対処が違う。
            if pairs:
                self.audit("remove_refused", resource=resource_id, reason="nonce が一致しない")
                return RemovalResult.NOT_OWNED
            return RemovalResult.ABSENT

        path, _ = found[0]
        return self._capture_and_remove(
            path,
            expect_nonce=expect_nonce,
            resource_id=resource_id,
            reason=reason,
        )

    def remove_matching(self, path: Path, expected: Entry, *, reason: str) -> RemovalResult:
        """**中身が一致するときだけ**消す。nonce を持たない古い宣言のための経路。

        捕まえてから、読み直した中身が期待どおりかを確かめる。nonce が無いので
        照合は「資源・宣言時刻・宣言者」の三つ組で行う。一致しなければ元へ戻す。

        **無条件の unlink にしてはならない。** 古い置き場は資源ごとの固定パスなので、
        読んでから消すまでに別セッションが同じ名前を作り直しうる。
        """
        tombstone = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.taken")
        moved, error = _rename_with_retry(path, tombstone)
        if moved is MoveResult.ABSENT:
            return RemovalResult.ABSENT
        if moved is not MoveResult.MOVED:
            self.audit("remove_failed", resource=expected.resource, error=error)
            return RemovalResult.FAILED

        captured = _read_entry_at(tombstone)
        if captured is None or (
            captured.resource,
            captured.since,
            captured.session,
        ) != (expected.resource, expected.since, expected.session):
            self._restore(tombstone, path, expected.resource)
            self.audit(
                "remove_refused", resource=expected.resource, reason="捕まえた宣言が別物だった"
            )
            return RemovalResult.NOT_OWNED

        result, error = _unlink_with_retry(tombstone)
        if result is RemovalResult.FAILED:
            self.audit("tombstone_left", resource=expected.resource, error=error)
        self.audit("removed", resource=expected.resource, job=captured.job, reason=reason)
        return RemovalResult.REMOVED

    def _capture_and_remove(
        self,
        path: Path,
        *,
        expect_nonce: str,
        resource_id: str,
        reason: str,
        audit_as: str | None = "removed",
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
        # **相乗りの取り下げでは書かない。** `removed` は rb history と audit_report の
        # 双方で**主宣言の解放**として読まれる。相乗り 1 人の離脱が保持者の claimed を
        # 閉じてしまい、まだ走っている宣言が「解放済み」に見える（掲示板は正しいのに
        # 監査だけが嘘をつく）。呼び出し側が join_removed を書くのでここは黙る。
        if audit_as:
            # **job も残す。** `rb history` は宣言と解放を (資源, job) で突き合わせる。
            # 資源だけで対応させると、同じ資源に並ぶ別の作業の解放を自分の宣言に
            # 結び付けてしまい、実所要が無関係な値になる。
            self.audit(audit_as, resource=resource_id, job=captured.job, reason=reason)
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

    def remove_own(
        self, resource_id: str, *, reason: str, nonce: str | None = None, cwd: str | None = None
    ) -> OwnRemoval:
        """**自分の**宣言を消す。結果は 3 つに分けて返す（:class:`OwnRemoval`）。

        平坦化する前は「主宣言か相乗りか」を選ばせていた。役割を記録しないので、
        選ばせるものが無くなった——**自分の宣言を消す**、それだけである。同じ資源へ
        並行して 2 本出していれば 2 本とも消える（``nonce`` を渡せばその 1 本だけ）。

        削除は 1 件ずつ nonce の CAS に乗せる。読んでから消すまでに別セッションが
        取り直しても、**新しい宣言を消さない**。

        ロックが取れないときは**囲わずに続行する**。解放できずに宣言を残すほうが
        有害であり、CAS という主防御は失われない。
        """
        removed: list[Entry] = []
        failed: list[Entry] = []
        swapped: list[Entry] = []
        foreign: list[Entry] = []
        with self.locked(resource_id) as lock:
            if lock is not LockState.ACQUIRED:
                self.audit("remove_unlocked", resource=resource_id, lock=str(lock))
            for path, entry in self.pairs_for(resource_id):
                if nonce is not None and entry.nonce != nonce:
                    foreign.append(entry)
                    continue
                if not self.owns(
                    entry, nonce=nonce, cwd=cwd, session_id=platform_info.session_id()
                ):
                    foreign.append(entry)
                    continue
                if entry.nonce:
                    # **CAS の入り口を 1 つに寄せる。** ここだけ `_capture_and_remove` を
                    # 直に呼ぶと、公開の入り口を差し替えても効かない経路ができる。
                    result = self.remove_if_nonce(
                        resource_id, expect_nonce=entry.nonce, reason=reason
                    )
                else:
                    # nonce を持たない古い宣言。**無条件に消さない**——固定パスなので、
                    # 読んでから消すまでに別セッションが同じ名前を作り直しうる。
                    result = self.remove_matching(path, entry, reason=reason)
                if result is RemovalResult.REMOVED:
                    removed.append(entry)
                elif result is RemovalResult.ABSENT:
                    pass  # 読んだ直後に誰かが消した。**残っていない**ので失敗ではない
                elif result is RemovalResult.NOT_OWNED:
                    # 読んでから消すまでに入れ替わった。**新しい宣言を消さなかった**、
                    # というのがここで守れた性質である。
                    swapped.append(entry)
                else:
                    failed.append(entry)
        return OwnRemoval(removed=removed, failed=failed, swapped=swapped, foreign=foreign)

    def list_all(self) -> list[Entry]:
        """全ての宣言を読む。読めなかったものは飛ばす。"""
        return [entry for _, entry in self.declarations()]

    def list_all_detailed(self) -> tuple[list[Entry], bool]:
        """全ての宣言と、**読めなかったものがあったか**を返す。"""
        found, unreadable = self.declarations_detailed()
        return [entry for _, entry in found], unreadable

    def declare(self, entry: Entry) -> bool:
        """宣言を 1 件、掲示板に残す。残せたら True。

        **断らない。** 資源を使っている作業が既にあっても、宣言はもう 1 件増えるだけで
        ある。断るかどうかを決めるのは掲示板ではなく呼び出し側で、その根拠は「誰が
        先に取ったか」ではなく**申告された実測**である（``--found free`` と言いながら
        生きた宣言があるなら、その申告か掲示板のどちらかが古い）。

        ファイル名は nonce なので、``O_EXCL`` の作成は必ず成功する。ここでの
        ``O_EXCL`` は**同じ宣言を二重に書かない**ためだけに残してある。
        """
        try:
            self.entries_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.audit("mkdir_failed", resource=entry.resource, error=str(exc))
            return False

        payload = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if not self._create_exclusively(
            self.declaration_path(entry.nonce), payload, entry.resource
        ):
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

    def replace(
        self, entry: Entry, *, reason: str, expect_nonce: str | None = None
    ) -> UpdateResult:
        """既存のエントリを差し替える。取得ではなく**更新**である。

        ``declare`` と違い ``O_EXCL`` は使わない。ここは宣言を増やすのではなく、
        既に出している宣言の書き換えだからである。取り違えないよう、
        **宣言を増やすのは ``declare`` だけ**が担う。

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
        # **書き換える 1 件を中身で引く。** 場所を名前から組み立てない（旧い置き場に
        # ある宣言も同じ経路で書き換えられる、という副次的な利点もある）。
        wanted = expect_nonce if expect_nonce is not None else entry.nonce
        found = [path for path, e in self.pairs_for(entry.resource) if e.nonce == wanted]
        if expect_nonce is not None and not found:
            self.audit("update_conflict", resource=entry.resource, expected=expect_nonce)
            return UpdateResult.CONFLICT

        path = found[0] if found else self.declaration_path(entry.nonce)
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

    def remove_all(self, resource_id: str, *, reason: str) -> int:
        """その資源の宣言を**全部**消す。消せた件数を返す。``--force`` の実体である。

        所有を見ない。見ないことが ``--force`` の意味である。
        """
        removed = 0
        for path, entry in self.pairs_for(resource_id):
            result, error = _unlink_with_retry(path)
            if result is RemovalResult.REMOVED:
                removed += 1
                self.audit("removed", resource=resource_id, job=entry.job, reason=reason)
            elif result is RemovalResult.FAILED:
                self.audit("remove_failed", resource=resource_id, error=error)
        return removed

    def audit(self, event: str, **fields: object) -> None:
        """監査ログに 1 行追記する。失敗しても黙って諦める。"""
        audit.append(self.root, event, **fields)


def first_declaration(board: Board, resource_id: str) -> Entry | None:
    """その資源の**最初の宣言**（無ければ None）。

    平坦化で「主宣言」は無くなった。表示や説明のために 1 件だけ挙げたい場面のための
    補助であり、**判断には使わない**（判断は宣言 1 件ずつ独立している）。
    """
    found = board.list_for(resource_id)
    return found[0] if found else None


def build_entry(
    resource_id: str,
    *,
    job: str,
    display: str = "",
    log: str | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    session: str | None = None,
    session_id: str | None = None,
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
            # **同じ場所で動く 2 つのセッションを区別する唯一の材料。** cwd も
            # `session`（cwd のベース名）も一致するため、これが無いと自分の宣言を
            # 消したつもりで他人の宣言を消せる。取れなければ空文字（cwd 判定へ落ちる）
            "session_id": platform_info.session_id() if session_id is None else session_id,
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
    # **機械の値を後に置く。** 先に置くと申告側の ``at`` が上書きし、LLM が書いた時刻が
    # そのまま掲示板に載る。「時刻はすべて機械生成」（DESIGN.md「Time Handling」）は
    # 引数の順序 1 つで破れる。
    return {**observed, "at": clock.now_iso()}
