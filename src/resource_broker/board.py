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
from collections.abc import Callable, Iterator
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
#: 本ツールが壊れてユーザーの作業を止めてはならないので、
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


class LockState(StrEnum):
    """ロックの取得結果。

    **3 値にするのは、インフラの故障と資源の競合を混同しないためである**。
    2 値にすると「ロックのディレクトリが作れない」
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

    「無い」「他人のもの」「失敗した」「確認できない」を区別する。畳むと、
    共有違反で消せなかっただけなのに「宣言が自分のものではなくなっています」という
    **事実と違う説明**を出すことになる。
    """

    REMOVED = "removed"
    ABSENT = "absent"
    NOT_OWNED = "not_owned"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    """**消せなかったのではなく、消せたかどうかを確認できなかった。**

    ``FAILED``（掲示板は読めた上で I/O が失敗した）と混ぜない。こちらは削除直後の
    再確認そのものが掲示板の一部を読めずに終わったケースで、「使用中で消せなかった」
    （``EXIT_BUSY`` 相当）とは終了コードの意味が違う——``EXIT_BROKEN``（内部の故障で
    操作を完了できなかった）に当たる（issue #18 指摘 4）。以前はここも ``FAILED`` に
    畳んでいたため、CLI が一律 ``EXIT_BUSY`` へ変換し、「確認できていない」を
    「使用中だと確認した」と偽って報告していた。"""


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


@dataclass(frozen=True)
class CleanResult:
    """``rb release --clean`` の結果。**畳んではならない。**

    以前は「消せた件数」しか返さず、CLI は常に ``EXIT_OK`` を返していた——
    走査そのものができなかった（ディレクトリが読めない）場合でも「読めない
    ファイルはありませんでした」と積極的な成功表現になっていた（issue #18
    指摘 5）。``complete`` を持たせることで、「確認済みの壊れたファイルは
    消しつつ、走査が不完全なら成功と言わない」を型で表現する。
    """

    removed: list[Path]
    """消せた（確認できた壊れたファイルのうち）。"""

    failed: list[Path]
    """壊れていると確認できたが、消せなかった（共有違反など）。"""

    complete: bool
    """掲示板ディレクトリの走査を完全に行えたか。

    ``False`` のとき、``removed`` に載っていない壊れたファイルが他にある
    かもしれない——「読めないファイルはありませんでした」と言ってはならない。
    """


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
#:
#: **``display`` はここに含めない（意図的）。** 表示名を添える仕組みそのものを廃止した
#: （issue #9）ので、既に掲示板にある ``display`` を読む・消す・特別に扱う必要は無い
#: ——``_KNOWN_KEYS`` から外れているだけで、上の前方互換の仕組みがそのまま ``extra`` へ
#: 退避して保存する。書き戻しても既存の宣言を壊さない。
_KNOWN_KEYS = frozenset(
    {
        "schema",
        "resource",
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


def _describe_special_node(entry: os.DirEntry) -> str:
    """``*.json`` という名前なのに通常ファイルでもリンクでもないノードの種類を言葉にする。

    監査ログに残すのは「読めなかった」だけでは足りない——運用側が現地で
    何を消せばよいか分かるように、種類も添える（issue #18 指摘 9）。
    """
    try:
        if entry.is_dir():
            return "ディレクトリ"
    except OSError:
        pass
    return "特殊ファイル"


def _json_files(
    directory: Path, *, on_anomaly: Callable[[Path, str], None] | None = None
) -> tuple[list[Path], bool]:
    """ディレクトリ内の ``*.json`` を並べる。**「読めない」を「空」と混ぜない。**

    ``Path.glob`` を使ってはならない。``glob`` は ``OSError`` を内部で握り潰して
    空を返すため、次のいずれも**空の掲示板と同じ形**で返ってくる。

    - 掲示板のディレクトリが通常ファイルになっている
    - 権限で拒否されている（ACL、別ユーザーの所有）
    - 切断されたネットワークパスを ``RESOURCE_BROKER_HOME`` に指している

    「空きだ」と断定して全セッションへ配るのは、このツールが最もやってはならない
    ことである（DESIGN.md「Ghost Detection」の非対称性の裏返し）。``os.scandir``
    は ``NotADirectoryError`` / ``PermissionError`` をそのまま投げるので区別できる。

    Parameters
    ----------
    on_anomaly : callable, optional
        ``*.json`` という名前なのに通常ファイルでもシンボリックリンクでも
        ない（ディレクトリ・FIFO・デバイスファイル等）ノードを見つけたときに
        呼ぶ。監査ログへ残すのは呼び出し側の責務とする（``Board`` を持たない
        この関数の責務にしない）。

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
                if on_anomaly:
                    on_anomaly(Path(entry.path), "壊れたリンク")
                continue
            # **通常ファイルでもシンボリックリンクでもない ".json" 名のノード。**
            # ディレクトリ・FIFO・デバイスファイルなど。以前は ``is_file()`` と
            # ``is_symlink()`` の両方が偽になるこの場合を黙って読み飛ばし、
            # ``complete=True`` のまま確定させていた——`glob` が握り潰していた
            # のと同じ穴を ``scandir`` で開け直していたことになる（issue #18
            # 指摘 9）。宣言として読めない以上「無かった」と同じに畳んではならない。
            unreadable = True
            if on_anomaly:
                on_anomaly(Path(entry.path), _describe_special_node(entry))
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


class PartialListingError(RuntimeError):
    """**不完全な列挙から、削除できる選択を作ろうとした。**

    :meth:`BoardListing.confirmed` だけが送出する。掲示板の一部が読めていない
    状態から「削除してよい個体の並び」を取り出そうとするのは、3 度のレビューで
    繰り返し出た欠陥（完全性を確認し忘れたまま破壊的操作へ進む）そのものである
    ——ここを例外にすることで、**その形のコードは実行時に必ず落ちる**ようにする。
    """


@dataclass(frozen=True)
class ConfirmedEntry:
    """**削除の公開入口へ渡せる、確認済みの選択 1 件。**

    低水準の CAS（``Board._remove_if_nonce``）は private にしてある。
    外から直接呼べる限り、呼び出し側が「掲示板を完全に
    読めたか」の確認を**忘れられる**——3 回のレビューで毎回別の経路がこれを
    やっていた（issue #18）。この型を経由しなければ :meth:`Board.remove_confirmed`
    は呼べないので、確認を忘れるという事態がそもそも起こらない。

    作れる場所は 2 つだけ。

    1. :meth:`BoardListing.confirmed` —— 掲示板を**完全に**読めた列挙から。
       読めていなければ :class:`PartialListingError` を送出し、1 件も作らせない
    2. :meth:`Board.confirm_own_declaration` —— **自分がいま書いた**宣言。
       列挙を経由しないので、そもそも「完全性」という概念が要らない
       （自分で書いた実体を、書いた直後に指すだけである）

    ``path`` と ``entry`` を対で持つのは、**削除は選択したその実体に対して行う**
    ためである。削除の直前に資源やファイル名から再列挙すると、選択と削除の間に
    掲示板の一部が読めなくなっていても気づけない（issue #17 指摘 2）。
    """

    path: Path
    entry: Entry


@dataclass(frozen=True)
class BoardListing:
    """掲示板を列挙した結果。**tuple では返さない。**

    以前は ``(entries, unreadable)`` という tuple で返していた。tuple は
    ``entries, _ = board.list_all_detailed()`` のように第 2 要素を簡単に捨てられ、
    実際にそれで「読めなかったものがあるか」という情報が失われ、破壊的操作が
    読めない掲示板を「空」「一意」と誤認する欠陥につながった（issue #17 指摘 1）。
    フィールドを持つ型にして、捨てるなら ``.pairs`` / ``.entries`` と明示させる。
    """

    pairs: list[tuple[Path, Entry]]
    """読めた宣言（パスつき）。古い順とは限らない——並び順は呼び出し元の責務。"""

    complete: bool
    """**理由を問わず** ``Entry`` にできなかったファイルが 1 つも無かったか。

    ``False`` になるのは、I/O で読めない・不正な UTF-8・JSON が壊れている・
    必須フィールドが欠けている・``*.json`` という名前なのに通常ファイルでも
    リンクでもない（ディレクトリ・特殊ファイル）、のいずれかが 1 件でもあった
    ときである。理由の違いは「破壊的操作の判断材料としてこの列挙を信じてよいか」
    を変えない——読めなかった 1 件に、探している宣言が隠れているかもしれない
    という点は理由によらず同じだからである。**理由じたいは監査ログに個別で残る**
    （``entry_unreadable`` / ``entry_corrupt``。DESIGN.md「Corrupt Entries」）。
    """

    @property
    def entries(self) -> list[Entry]:
        """``Entry`` だけを取り出す。パスが要らない読み手のための便宜。"""
        return [entry for _, entry in self.pairs]

    def confirmed(self) -> list[ConfirmedEntry]:
        """**削除できる選択の並びを作る唯一の経路（列挙側）。**

        ``complete`` が ``False`` なら :class:`PartialListingError` を送出し、
        1 件も返さない。「読めなかった側に、探している宣言や、いま消そうと
        している宣言の生きた入れ替わり先が隠れているかもしれない」という
        懸念は、資源で絞る前も後も、``--force`` かどうかにも関わらず同じで
        ある——このメソッド以外に :class:`ConfirmedEntry` を作る経路を
        列挙側に持たないことで、「完全性の確認を忘れる」という形のコードが
        そもそも書けなくなる。

        戻り値は ``self.pairs`` と同じ並び・同じ長さである（1 対 1 で対応する）。
        """
        if not self.complete:
            raise PartialListingError(
                "掲示板の一部が読めていない列挙からは、削除できる選択を作れない"
                "（read されなかった側に探している宣言が隠れているかもしれない）"
            )
        return [ConfirmedEntry(path=path, entry=entry) for path, entry in self.pairs]


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

    unconfirmed: list[Entry] = field(default_factory=list)
    """消せたかどうかを**確認できなかった**もの（掲示板の一部が削除直後に読めない）。

    ``failed``（掲示板は読めた上で I/O が失敗した）と畳まない。呼び出し側の
    終了コードが違う——``failed`` は「使用中で消せなかった」（``EXIT_BUSY``）、
    こちらは「消せたか確認できていない」（``EXIT_BROKEN``）である
    （issue #18 指摘 4）。"""

    @property
    def any_here(self) -> bool:
        """その資源に宣言が 1 件でもあったか。"""
        return bool(
            self.removed or self.failed or self.swapped or self.foreign or self.unconfirmed
        )


@dataclass(frozen=True)
class ForcedRemoval:
    """``--force`` で列挙した個体を 1 件ずつ消した結果。**畳んではならない。**

    ``OwnRemoval`` と違い ``foreign`` は無い——``--force`` は所有を問わないので、
    「自分のものではないから触らなかった」という状態自体が存在しない。それでも
    「消せた」「消せなかった」「確認できなかった」「入れ替わっていた」は畳まない。

    **表示・監査はこの 1 つの結果から作ること。** 列挙（表示用）と削除を別々に
    行うと、表示した対象と実際に消した対象が食い違いうる
    （issue #15 指摘 12・issue #18 末尾）。:meth:`Board.remove_selected` は
    渡された選択の並びをそのまま 1 件ずつ消すので、この結果の 4 つのリストを
    合わせれば渡した並びと過不足なく対応する。
    """

    removed: list[Entry]
    unconfirmed: list[Entry]
    swapped: list[Entry]
    """選択した実体が、削除を試みた時点で既に別の宣言へ入れ替わっていた
    （他セッションが同じ場所を取り直した）。``--force`` でも**選択したその実体
    以外は消さない**——CAS が保証する性質はここでも変わらない。"""
    failed: list[Entry]

    @property
    def any_here(self) -> bool:
        """対象に選ばれたものが 1 件でもあったか。"""
        return bool(self.removed or self.unconfirmed or self.swapped or self.failed)


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
        CAS（:meth:`remove_confirmed`）が守っており、ロックの有無に依存しない。

        **取れなければ排他は無い。** その場合でも通す——ロックが取れないのは本ツール側の
        事情であって「資源が使用中だと確認できた」ではなく、そこで止めるのは fail-open に
        反する。**保証の範囲はロックが取れる間だけ**であり、取れなかったことは呼び出し側が
        必ず告げる。ロックを主防御に据える代償として、Windows で
        最悪の相関を抱えることになる。

        結果は 3 値で渡す（:class:`LockState`）。**インフラの故障と資源の競合を
        混同しない**。ただし**どの値でも呼び出し側は止まらない**。
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

        旧い固定パス形式（``board/<資源>.json``）の宣言も、このディレクトリを走査する
        だけで**同じ宣言として読める**（nonce を鍵にした平坦なファイル名と、ファイルの
        置き場所そのものは変わっていない）。ただし ``board/joins/`` という**別の
        ディレクトリ**の走査はしない——旧形式の相乗りを見失わないための経路だったが、
        監査ログで宣言の寿命を実測すると中央値 5.4 分・最長 2.1 時間だった（issue #9）。
        その短い窓のためだけに、もう 1 本の削除経路と走査が掲示板の複雑さとして
        残り続ける理由が無い。
        """
        return self.declarations_detailed().pairs

    def declarations_detailed(self) -> BoardListing:
        """全ての宣言と、**完全性**（:attr:`BoardListing.complete`）を返す。

        **理由を問わず、1 件でも ``Entry`` にできなければ ``complete=False``。**
        以前は I/O で読めない場合（``entry_unreadable``）だけを見て、JSON の破損
        （``entry_corrupt``）や必須フィールドの欠落は「完全に読めた」側へ黙って
        含めていた。壊れたファイルに、探している宣言が一致していないとは
        証明できない以上、理由による差は無い（issue #17 指摘 1）。

        **監査には理由を残す。** ``entry_unreadable``（I/O。共有違反など一時的な
        事象かもしれない）と ``entry_corrupt``（中身が壊れている）は畳まない
        ——``--clean`` の対象判定（:meth:`unreadable_paths`）が引き続きこの
        区別を使うためである（DESIGN.md「Corrupt Entries」）。畳むのは
        「破壊的操作の判断材料としての完全性」だけである。
        """
        found: list[tuple[Path, Entry]] = []

        def report_anomaly(path: Path, kind: str) -> None:
            self.audit("entry_unreadable", path=str(path), kind=kind)

        paths, unreadable = _json_files(self.entries_dir, on_anomaly=report_anomaly)
        complete = not unreadable
        for path in sorted(paths):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                # 読めないのは「壊れている」とは別の事実である。**空だと言わない側。**
                self.audit("entry_unreadable", path=str(path), error=str(exc))
                complete = False
                continue
            except (UnicodeDecodeError, ValueError) as exc:
                # **不正な UTF-8 は「読めない」ではなく「壊れている」側。** バイト列は
                # 取れているので I/O の失敗ではなく、中身が正規の形をしていない
                # ——JSON デコード失敗と同じ扱いにする。
                self.audit("entry_corrupt", path=str(path), error=str(exc))
                complete = False
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                self.audit("entry_corrupt", path=str(path), error=str(exc))
                complete = False
                continue
            entry = Entry.from_dict(data)
            if entry is None:
                # **JSON としては読めたが、宣言の形を最低限すら満たさない。**
                # これも「完全に読めた」に含めてはならない——`resource` が
                # 読めない以上、この 1 件がどの資源のものか分からない。
                self.audit("entry_corrupt", path=str(path), reason="必須フィールドが読めない")
                complete = False
                continue
            found.append((path, entry))
        return BoardListing(pairs=found, complete=complete)

    def unreadable_paths(self) -> list[Path]:
        """**どの資源にも紐づけられないファイル**を返す。**走査の完全性は捨てる。**

        表示・削除以外の一覧用途（``rb status`` / ``rb claim`` の案内）はこちらで
        十分である。走査そのものが不完全だったかを見る必要がある場面
        （``rb release --clean``）は :meth:`unreadable_paths_detailed` を使うこと。
        """
        return self.unreadable_paths_detailed()[0]

    def unreadable_paths_detailed(self) -> tuple[list[Path], bool]:
        """**どの資源にも紐づけられないファイル**と、**走査を完全に終えられたか**を返す。

        壊れていて ``resource`` が読めない宣言は、資源を指して消すことができない。
        平坦化する前は ``board/<資源>.json`` という名前だったので名指しで消せたが、
        ファイル名が身元を持たなくなった以上、**中身が読めなければ持ち主も分からない**。

        塞ぐものは無い（宣言のファイル名は nonce なので、壊れたファイルがあっても
        新しい宣言は作れる）。残るのは掃除の手段だけなので、**場所を教える**。

        Returns
        -------
        tuple of (list of Path, bool)
            壊れていると確認できたファイルと、**ディレクトリの走査自体が完全に
            行えたか**。``False`` のとき、ディレクトリ自体が読めない（権限・
            切断されたネットワークパス等）ので、**この一覧に載っていないだけの
            壊れたファイルが他にあるかもしれない**——``rb release --clean`` が
            「読めないファイルはありませんでした」と断定してはならない理由
            そのものである（issue #18 指摘 5）。
        """
        found: list[Path] = []
        complete = True

        def report_anomaly(path: Path, kind: str) -> None:
            self.audit("entry_unreadable", path=str(path), kind=kind)

        paths, unreadable = _json_files(self.entries_dir, on_anomaly=report_anomaly)
        if unreadable:
            # **走査そのものが崩れた。** 個別ファイルの壊れ方とは別の事象で、
            # 「見つけたものが全部」とは言えなくなる。
            complete = False
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                # **「読めない」を「壊れている」と混ぜない。** 他セッションが捕獲の
                # 途中で名前を外した瞬間（`FileNotFoundError`）や、一時的な共有違反
                # （`PermissionError`）は、**生きた宣言**でも起こる。ここへ入れると
                # `--clean` がそれを消し、掲示板は空・資源は掴まれたままになる。
                continue
            except (UnicodeDecodeError, ValueError):
                # **不正な UTF-8 は「壊れている」側。** バイト列は取れているので
                # 生きた宣言が一時的に読めないケースとは違う。JSON デコード失敗と
                # 同じ扱いにする（``declarations_detailed`` と揃える）。
                found.append(path)
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                found.append(path)
                continue
            if Entry.from_dict(data) is None:
                found.append(path)
        return sorted(found), complete

    def remove_unreadable(self, *, reason: str) -> CleanResult:
        """読めないファイルを消す。**畳んではならない**（:class:`CleanResult`）。

        資源を指定しない。**指定できない**——中身が読めないので、どの資源のものか
        分からない。``rb release --force`` が資源ごとに動くのと対照的だが、
        それは対象が「誰のものでもないゴミ」だからである。
        """
        removed: list[Path] = []
        failed: list[Path] = []
        targets, complete = self.unreadable_paths_detailed()
        for path in targets:
            # **消す直前にもう一度確かめる。** 一覧を作ってから消すまでの間に、正常な
            # 宣言がそこへ現れうる（旧い置き場は資源ごとの固定パスである）。
            current, current_complete = self.unreadable_paths_detailed()
            if not current_complete:
                complete = False
            if path not in set(current):
                continue
            result, error = _unlink_with_retry(path)
            if result is RemovalResult.REMOVED:
                removed.append(path)
                self.audit("unreadable_removed", path=str(path), reason=reason)
            elif result is RemovalResult.FAILED:
                failed.append(path)
                self.audit("remove_failed", path=str(path), error=error)
        return CleanResult(removed=removed, failed=failed, complete=complete)

    def list_for(self, resource_id: str) -> list[Entry]:
        """その資源の宣言を**古い順**に返す。

        順序は ``since`` で決まる。「どれが先に取ったか」を別に記録しない——
        時間的に後のものが後から来たに決まっている。**完全性は捨てる**
        （読めなかったものがあっても黙って飛ばす）。完全性を見る場面は
        :meth:`pairs_for_detailed` を使うこと。
        """
        return [entry for _, entry in self.pairs_for(resource_id)]

    def pairs_for(self, resource_id: str) -> list[tuple[Path, Entry]]:
        """その資源の宣言を、パスつきで古い順に返す。**完全性は捨てる。**"""
        return self.pairs_for_detailed(resource_id).pairs

    def pairs_for_detailed(self, resource_id: str) -> BoardListing:
        """その資源の宣言を、パスつき・古い順・**完全性つき**で返す。

        破壊的操作（``release`` の各経路）はここを使う。**完全性は掲示板全体を
        基準にする**——資源で絞ったあとに「揃って見える」かどうかでは判断しない。
        読めなかったファイルは中身が読めていない以上、それがこの資源のもの
        ではないと言い切れない。資源で先に絞ってから完全性を見ると、絞る前の
        段階で失われた情報を「たまたま全部読めた」と取り違える（issue #17 指摘 1）。
        """
        listing = self.declarations_detailed()
        found = [(path, e) for path, e in listing.pairs if e.resource == resource_id]
        found.sort(key=lambda item: (item[1].since, str(item[0])))
        return BoardListing(pairs=found, complete=listing.complete)

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
        古い宣言（session_id を持たない）や Claude Code 以外からの利用を締め出さないためである。

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

    def _remove_if_nonce(
        self,
        resource_id: str,
        *,
        expect_nonce: str,
        reason: str,
        known: tuple[Path, Entry] | None = None,
    ) -> RemovalResult:
        """**期待する nonce と一致するときだけ**消す（compare-and-swap）。**private。**

        低水準の CAS そのものである。外から直接呼べる限り、呼び出し側が
        「掲示板を完全に読めたか」の確認を忘れられる——3 回のレビューで毎回
        別の経路がこれをやっていた（issue #18）。公開の削除入口は
        :meth:`remove_confirmed` だけであり、そちらは :class:`ConfirmedEntry`
        （完全性を確認した列挙、または自分が書いた宣言からしか作れない）を
        要求する。この関数自身はモジュール内の信頼できる呼び出し元
        （``remove_confirmed`` 自身と、テストで直接 CAS の性質を検査する箇所）
        だけが使う。

        無条件の ``unlink`` は、読んだエントリと消すエントリが同じである保証を持たない。
        A と B が同じ幽霊を見て、A が「消して取る」に成功した直後に B が消すと、
        **B は A の生きた宣言を消して**自分も取得に成功する。掲示板が防ぐと宣言している
        二重取得そのものである。ロックで囲っても、ロックが外れた瞬間だけこの穴が開く。

        そこで削除を条件付きにする。手順は 3 段で、正しさは 2 段目の**原子性**に乗る。

        1. 安く先読みして、明らかに別物なら何も動かさずに諦める
        2. ``os.rename`` で一時名へ**捕まえる**。成功できるのは 1 人だけである
           （成功した瞬間に元の名前は消えるので、同時に走った他方は「無い」になる）
        3. 捕まえた中身の nonce を確かめ、一致すれば消す。違えば**元へ戻す**

        Parameters
        ----------
        known : tuple of (Path, Entry), optional
            呼び出し側が**完全性を確認した列挙**（:meth:`pairs_for_detailed` など）
            から既に持っている、消したい実体そのもの。渡された場合は 1 段目の
            先読み（``pairs_for`` による再列挙）を行わない——再列挙は、選択の
            直後に掲示板の一部が読めなくなっていても気づけず、実際には存在する
            宣言を「無い」「別物になっている」と誤って断定しうる（issue #17
            指摘 2）。選択に使ったのと同じ実体をそのまま 2 段目（捕獲）へ渡すことで、
            選択と削除の間で完全性の情報を捨て直さない。2〜3 段目の正しさ
            （原子的な捕獲と nonce の再確認）は ``known`` の有無に関わらず同じである
            ——古くなった ``known`` を渡しても、捕獲後の nonce 照合が誤りを防ぐ。
            捕獲が ``ABSENT``（選択した実体そのものが既に居ない）に終わったときは、
            「本当に無い」か「別の宣言に入れ替わった」かを見分けるために、この
            資源だけを対象とした 1 回きりの再確認を行う。その再確認自体が
            不完全なら「無い」と断定せず ``UNCONFIRMED`` を返す——「消せなかった」
            （``FAILED``）とも「使用中」（``NOT_OWNED``）とも違う、**確認そのものが
            取れていない**という第 3 の状態である（issue #18 指摘 4）。

        Returns
        -------
        RemovalResult
            消した / 無かった / 別物だった / 消せなかった / 確認できなかった。

        Notes
        -----
        **幽霊の退去は読み手の圧力で失敗しうる。** 2 段目の ``os.rename`` は、
        Windows では他プロセスが読んでいる最中のファイルに対して ``PermissionError``
        になる（Python の ``open()`` は ``FILE_SHARE_DELETE`` を付けないため）。
        フックが全セッションの全プロンプトで掲示板を読むので、これは例外的な事態ではない。
        数回やり直して吸収し、吸収できなければ ``FAILED`` を返して**保守的に諦める**
        （消せていないのに消えたと答えるより、退けられなかったと答えるほうが安全である）。
        """
        if not expect_nonce:
            # **空文字を鍵にしてはならない。** nonce を持たない古い宣言は複数ありうるので、
            # 「nonce が空のもの」に一致させると**別の生きた宣言**を捕まえて消す。
            # 呼び出し元は :meth:`remove_confirmed` であり、nonce を持たない宣言を
            # ここへ渡してくるのは ``--force``（:meth:`remove_selected`）だけである
            # ——そちらは個体の照合をそもそも要求しない（``_remove_unkeyed`` 参照）。
            self.audit("remove_refused", resource=resource_id, reason="nonce が空である")
            return RemovalResult.NOT_OWNED

        if known is not None:
            path, entry = known
            if entry.nonce != expect_nonce:
                # 呼び出し側の取り違え。念のため確かめる（実害は無いはずだが、
                # 黙って別物を捕獲しにいくよりは早く気づけるほうがよい）。
                self.audit("remove_refused", resource=resource_id, reason="nonce が一致しない")
                return RemovalResult.NOT_OWNED
            result = self._capture_and_remove(
                path, expect_nonce=expect_nonce, resource_id=resource_id, reason=reason
            )
            if result is not RemovalResult.ABSENT:
                return result
            # **ABSENT を早合点しない。** 選択に使った実体そのものは居なくなって
            # いても、この資源に**別の宣言**（他セッションが取り直した）が
            # 残っているかもしれない——それは「無い」ではなく「入れ替わった」で
            # あり、対処が違う。この確認だけは再列挙するが、選択の直前ではなく
            # 削除が ABSENT に終わったあとの一度きりなので、選択時に確認した
            # 完全性を日常的に捨て直す経路にはならない（issue #17 指摘 2 と対）。
            listing = self.pairs_for_detailed(resource_id)
            if not listing.complete:
                # **「無い」と「確認できない」を分ける。** `UNCONFIRMED` は
                # `FAILED`（掲示板は読めた上で I/O が失敗した）とは別の意味を持つ
                # ——CLI の終了コードが違う（issue #18 指摘 4）。監査ログにも
                # 理由を残すので、「本当に無い」との違いは追跡できる。
                self.audit(
                    "remove_unconfirmed",
                    resource=resource_id,
                    reason="削除直後の再確認で掲示板の一部が読めない",
                )
                return RemovalResult.UNCONFIRMED
            return RemovalResult.NOT_OWNED if listing.pairs else RemovalResult.ABSENT

        # 1. 先読み。**中身で探す。** ファイル名から場所を組み立てない——名前の付け方を
        #    変えた瞬間に「見つからない」へ黙って倒れる（実際に一度そうなった）。
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

    def _remove_unkeyed(self, path: Path, *, resource_id: str, reason: str) -> RemovalResult:
        """nonce を持たない宣言を消す。``--force`` だけが通る経路。**private。**

        nonce が無いと個体を指す鍵が無いので、:meth:`_remove_if_nonce` のように
        「捕まえてから中身を確かめて、違えば戻す」という CAS は組めない——確かめる
        相手（期待する nonce）がそもそも無い。``--force`` は元から「見ずに全部消す」
        という意味であり、この形の宣言もその対象に含めてよい。確認の手段が無い
        ことを、確認しないことで受け入れる。

        以前はここに、捕まえた中身を「資源・宣言時刻・宣言者」の三つ組で照合してから
        消す :func:`_remove_matching`（削除済み）があった。監査ログで宣言の寿命を
        実測すると中央値 5.4 分・最長 2.1 時間・24 時間超はゼロ（issue #9）で、
        この形の宣言が生き残る窓はごく短い。その短い窓のためだけに、もう 1 本の
        削除経路と、それが呼ぶ ``joins/`` の走査が掲示板の複雑さとして残り、
        そこから欠陥が複数出た。単純な unlink に戻す。
        """
        result, error = _unlink_with_retry(path)
        if result is RemovalResult.FAILED:
            self.audit("remove_failed", resource=resource_id, error=error)
        elif result is RemovalResult.REMOVED:
            self.audit("removed", resource=resource_id, job="", nonce="", reason=reason)
        return result

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

        無条件の ``unlink`` は、読んだ内容と消す対象が同じである保証を持たない。
        読んでから消すまでの間に別プロセスが同じ名前を消して作り直すと、
        **新しい生きた申告を消す**。パスを引数に取って捕獲（``os.rename``）を
        経由するのは、この read-delete race を塞ぐためである。

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
            # **job も nonce も残す。** `rb history` は宣言と解放を突き合わせるとき、
            # 両方が nonce を持てば nonce だけで確実に対応させる。資源 + job だけで
            # 対応させると、同じ資源・同じ job の宣言が並行したとき別の宣言の解放を
            # 自分の宣言に結び付けてしまう——`--nonce` で片方だけ消しても、履歴は
            # 時刻順で先に来た方（消していない方）に解放を割り当ててしまう。
            self.audit(
                audit_as,
                resource=resource_id,
                job=captured.job,
                nonce=captured.nonce,
                reason=reason,
            )
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

    def remove_confirmed(
        self, selection: ConfirmedEntry, *, reason: str, force: bool = False
    ) -> RemovalResult:
        """**確認済みの選択**を消す。**個別の宣言を消す唯一の公開入口。**

        低水準の CAS（:meth:`_remove_if_nonce`）は private にしてある。ここは
        :class:`ConfirmedEntry` **でなければ呼べない**——渡すものが無ければ
        削除できないので、「完全性の確認を忘れる」という 3 回繰り返した欠陥の形が、
        そもそも書けなくなる（issue #18）。

        選択した実体（``selection.path`` / ``selection.entry``）に対してそのまま
        削除を試みる。**再列挙しない**——選択と削除の間で完全性の情報を
        捨て直さないことが、この設計の核心である。

        Parameters
        ----------
        force : bool, optional
            ``entry`` が nonce を持たない（旧形式の）宣言のとき、それでも消すか。
            **既定では消さない**——nonce が無いと個体を指す鍵が無く、``remove_own``
            や ``--nonce`` のような「この 1 件だけを狙う」経路がこの形の宣言に
            辿り着いても、安全に個体として確認する手段が無い（issue #9）。
            ``--force``（:meth:`remove_selected`）だけがこの形の宣言も対象に
            含める——「見ずに全部消す」がその意味だからである。
        """
        if not isinstance(selection, ConfirmedEntry):
            # **型を実行時にも守る。** 静的型検査を経ない呼び出し（動的な dispatch、
            # テストの誤用）でも、確認済みでない選択で削除が進まないようにする。
            raise TypeError(
                "remove_confirmed には ConfirmedEntry を渡すこと"
                "（BoardListing.confirmed() または confirm_own_declaration() で作る）"
            )
        path, entry = selection.path, selection.entry
        if entry.nonce:
            return self._remove_if_nonce(
                entry.resource, expect_nonce=entry.nonce, reason=reason, known=(path, entry)
            )
        if force:
            return self._remove_unkeyed(path, resource_id=entry.resource, reason=reason)
        # **個体として指せない（nonce が無い）。** `--force` 以外の経路（`remove_own`
        # 経由の自分の宣言の解放、`--nonce`）はここで拒否する。`rb status` には
        # 引き続き載るので「見えないまま残る」にはならない——消す手段が `--force` と
        # `--clean`（`remove_unreadable`。別経路）だけになるだけである。
        self.audit(
            "remove_refused", resource=entry.resource, reason="nonce が無い個体は指定できない"
        )
        return RemovalResult.NOT_OWNED

    def confirm_own_declaration(self, entry: Entry) -> ConfirmedEntry:
        """**自分がいま書いた**宣言を、削除できる選択にする。

        :meth:`BoardListing.confirmed` と並ぶ、:class:`ConfirmedEntry` を作る
        もう 1 つの経路。列挙を経由しないので「完全性」という概念そのものが
        要らない——``entry`` は呼び出し側が :meth:`declare` で自分自身が書いた
        実体であり、掲示板のどこを探すまでもなく居場所が分かっている
        （``rb run`` の後始末はここを使う。issue #18 指摘 2:
        「自分で作った entry と nonce を持っているのに再列挙している」の解消）。
        """
        return ConfirmedEntry(path=self.declaration_path(entry.nonce), entry=entry)

    def _remove_each(
        self, selections: list[ConfirmedEntry], *, reason: str, force: bool = False
    ) -> tuple[list[Entry], list[Entry], list[Entry], list[Entry]]:
        """選択の並びを 1 件ずつ、:meth:`remove_confirmed` で消す。

        ``remove_own``（所有で絞ってから呼ぶ）と ``remove_selected``（``--force``。
        絞らずに全件へ呼ぶ）が共有する実装——別々に実装すると、片方だけ直して
        もう片方を直し忘れる経路ができる（このプロジェクトが 3 回繰り返した形）。

        Parameters
        ----------
        force : bool, optional
            そのまま :meth:`remove_confirmed` へ渡す。``True`` は
            ``remove_selected``（``--force``）だけが渡し、nonce を持たない
            宣言も対象に含める。``remove_own`` は既定の ``False`` のまま
            ——個体として指せない宣言は、自分の所有物に見えても消さない。

        Returns
        -------
        tuple of (list of Entry, list of Entry, list of Entry, list of Entry)
            ``(removed, unconfirmed, swapped, failed)``。``ABSENT`` はどちらにも
            入れない——読んだ直後に誰かが消しただけで、**残っていない**ので
            失敗ではない。
        """
        removed: list[Entry] = []
        unconfirmed: list[Entry] = []
        swapped: list[Entry] = []
        failed: list[Entry] = []
        for selection in selections:
            result = self.remove_confirmed(selection, reason=reason, force=force)
            if result is RemovalResult.REMOVED:
                removed.append(selection.entry)
            elif result is RemovalResult.ABSENT:
                pass  # 読んだ直後に誰かが消した。**残っていない**ので失敗ではない
            elif result is RemovalResult.NOT_OWNED:
                # 読んでから消すまでに入れ替わった。**新しい宣言を消さなかった**、
                # というのがここで守れた性質である。
                swapped.append(selection.entry)
            elif result is RemovalResult.UNCONFIRMED:
                unconfirmed.append(selection.entry)
            else:
                failed.append(selection.entry)
        return removed, unconfirmed, swapped, failed

    def remove_own(
        self,
        resource_id: str,
        *,
        reason: str,
        declared: list[ConfirmedEntry],
        nonce: str | None = None,
        cwd: str | None = None,
    ) -> OwnRemoval:
        """**自分の**宣言を消す。結果は畳まずに返す（:class:`OwnRemoval`）。

        平坦化する前は「主宣言か相乗りか」を選ばせていた。役割を記録しないので、
        選ばせるものが無くなった——**自分の宣言を消す**、それだけである。同じ資源へ
        並行して 2 本出していれば 2 本とも消える（``nonce`` を渡せばその 1 本だけ）。

        削除は 1 件ずつ nonce の CAS に乗せる。読んでから消すまでに別セッションが
        取り直しても、**新しい宣言を消さない**。

        ロックが取れないときは**囲わずに続行する**。解放できずに宣言を残すほうが
        有害であり、CAS という主防御は失われない。

        Parameters
        ----------
        declared : list of ConfirmedEntry
            呼び出し側が**完全性を確認した列挙**（:meth:`BoardListing.confirmed`）
            から既に持っている、この資源の全宣言。**必須**であり省略できない
            ——ここを ``None`` 許容の任意引数にしていた頃は、呼び出し側の 1 つ
            （``rb run`` の自動解放）が実際に渡し忘れ、内部で ``pairs_for`` に
            よる再列挙が起きていた（issue #18 指摘 2）。自分がいま書いた宣言を
            消すだけなら列挙そのものが要らないので、``rb run`` の後始末は
            この関数を経由せず :meth:`remove_confirmed` を直接使うように改めた。
            結果としてこの関数の呼び出し元は「完全性を確認済みの列挙を持っている」
            場面だけになったので、引数を必須にできる。
        """
        with self.locked(resource_id) as lock:
            if lock is not LockState.ACQUIRED:
                self.audit("remove_unlocked", resource=resource_id, lock=str(lock))
            mine: list[ConfirmedEntry] = []
            foreign: list[Entry] = []
            for selection in declared:
                entry = selection.entry
                if nonce is not None and entry.nonce != nonce:
                    foreign.append(entry)
                    continue
                if not self.owns(
                    entry, nonce=nonce, cwd=cwd, session_id=platform_info.session_id()
                ):
                    foreign.append(entry)
                    continue
                mine.append(selection)
            removed, unconfirmed, swapped, failed = self._remove_each(mine, reason=reason)
        return OwnRemoval(
            removed=removed,
            failed=failed,
            swapped=swapped,
            foreign=foreign,
            unconfirmed=unconfirmed,
        )

    def remove_selected(
        self, resource_id: str, selections: list[ConfirmedEntry], *, reason: str
    ) -> ForcedRemoval:
        """``--force``: 渡された個体を、**所有を問わず** 1 件ずつ消す。

        **資源名だけで何件消えるか決まる公開入口は持たない。** 渡した
        ``selections`` 以外は対象にならない——:meth:`BoardListing.confirmed`
        で得た並びをそのまま渡すこと。``--force`` の意味（見ないことがその意味）は
        変えないが、**「何を消したか分からないまま消す」ことはしない**：

        - 列挙（表示用）と削除を同じ並びから行うので、表示した対象と実際に
          消した対象が食い違わない（以前は ``_release_forced`` が表示用に
          ``pairs_for_detailed`` で列挙し、削除は内部で ``pairs_for`` により
          別に列挙し直す ``remove_all`` を呼んでいた。issue #15 指摘 12・
          issue #18 末尾）
        - 各個体は CAS（:meth:`remove_confirmed`）を通るので、選択した実体
          以外は消えない。``--force`` でも、選択と削除の間に他セッションが
          その場所を取り直していれば ``swapped`` として区別される
        - **nonce を持たない宣言もここでは対象に含める**（``force=True``）。
          個体として指す鍵が無いので確かめる CAS は組めないが、``--force`` は
          元から「見ずに全部消す」という意味であり、この形の宣言だけを
          取り残さない（issue #9。消す手段が ``--force`` と ``--clean`` に
          絞られる代わり）
        """
        with self.locked(resource_id) as lock:
            if lock is not LockState.ACQUIRED:
                self.audit("remove_unlocked", resource=resource_id, lock=str(lock))
            removed, unconfirmed, swapped, failed = self._remove_each(
                selections, reason=reason, force=True
            )
        return ForcedRemoval(
            removed=removed, unconfirmed=unconfirmed, swapped=swapped, failed=failed
        )

    def list_all(self) -> list[Entry]:
        """全ての宣言を読む。読めなかったものは飛ばす。"""
        return [entry for _, entry in self.declarations()]

    def list_all_detailed(self) -> BoardListing:
        """全ての宣言と、**完全性**（:attr:`BoardListing.complete`）を返す。

        ``declarations_detailed`` と中身は同じである（別に持たない——同じ完全性を
        2 つの型で表現すると、片方だけ直して他方を直し忘れる経路ができる）。
        名前を分けているのは、呼び出し側の語彙（「宣言」ではなく「掲示板全体」）に
        合わせるためだけである。
        """
        return self.declarations_detailed()

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
        # **nonce も残す。** `rb history` は資源 + job で宣言と解放を対応付けるが、
        # 同じ資源・同じ job の宣言が並行すると job だけでは取り違える。nonce は
        # 宣言ごとに一意なので、両方が持っていれば nonce だけで確実に対応が付く
        # （`_pair_key` 参照）。nonce を持たない古いログとの互換のため、job も残す。
        self.audit(
            "claimed",
            resource=entry.resource,
            job=entry.job,
            nonce=entry.nonce,
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
        削除（:meth:`remove_confirmed`）と違い、ここでは「捕まえてから確かめる」形を採らない。
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
    JST と UTC の取り違えや単純な足し算の誤りが実際に起きているためである。
    ``30m`` のような期間表記が読めたときだけ
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
    # そのまま掲示板に載る。「時刻はすべて機械生成」は
    # 引数の順序 1 つで破れる。
    return {**observed, "at": clock.now_iso()}
