"""掲示板の読み書き。

掲示板は**資源ごとに 1 ファイル**の JSON である。単一ファイルに集約しないのは、
破損の被害を 1 資源に閉じ込めるためと、``O_EXCL`` による取得競合の解決を
単純にするためである（DESIGN.md「Architecture」）。

本モジュールの全ての公開関数は**例外を投げない**。読めない・書けない・壊れているは
すべて「情報が無い」に畳み込み、呼び出し側が fail-open で通せるようにする。
握りつぶした事実は監査ログに残す。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import audit, clock, naming, platform_info

SCHEMA = 1

#: 読み取り時に既知として扱うキー。これ以外は extra に退避して書き戻す（前方互換）。
_KNOWN_KEYS = frozenset(
    {"schema", "resource", "display", "holder", "log", "since", "boot", "observed"}
)


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
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def since_dt(self) -> datetime | None:
        """宣言時刻。解釈できなければ None。"""
        return clock.parse_iso(self.since)

    @property
    def pid(self) -> int | None:
        """宣言に書かれた PID。無ければ None。"""
        value = self.holder.get("pid")
        return value if isinstance(value, int) else None

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
        return cls(
            resource=resource,
            display=data["display"] if isinstance(data.get("display"), str) else "",
            holder=holder if isinstance(holder, dict) else {},
            log=data["log"] if isinstance(data.get("log"), str) else None,
            since=data["since"] if isinstance(data.get("since"), str) else "",
            boot=data["boot"] if isinstance(data.get("boot"), str) else None,
            observed=observed if isinstance(observed, dict) else None,
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

        payload = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            self.audit("claim_failed", resource=entry.resource, error=str(exc))
            return False

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
        except OSError as exc:
            self.audit("claim_write_failed", resource=entry.resource, error=str(exc))
            self.remove(entry.resource, reason="書き込みに失敗したため取り消した")
            return False

        self.audit("claimed", resource=entry.resource, job=entry.job, pid=entry.pid)
        return True

    def remove(self, resource_id: str, *, reason: str) -> bool:
        """エントリを削除する。存在しなければ False。理由は監査ログに残す。"""
        path = self.path_for(resource_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            self.audit("remove_failed", resource=resource_id, error=str(exc))
            return False
        self.audit("removed", resource=resource_id, reason=reason)
        return True

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
        },
        log=log,
        since=clock.now_iso(),
        boot=clock.to_iso(boot) if boot else None,
        observed=_stamp_observed(observed),
    )


def _stamp_observed(observed: dict[str, object] | None) -> dict[str, object] | None:
    """実測に観測時刻を刻む。

    「いつ観測したか」が無いと、掲示板に残った実測値がどの時点のものか分からない。
    時刻はここで機械生成する（DESIGN.md「Board Schema」の ``observed.at``）。
    """
    if observed is None:
        return None
    return {"at": clock.now_iso(), **observed}
