"""監査ログを俯瞰する。**運用の健全性を 1 コマンドで確かめるための開発用ツール。**

`rb status` は今を、`rb history` は 1 件ずつを見せる。どちらも「全体としてうまく回って
いるか」には答えない。それを毎回その場でスクリプトを書いて調べていたので、形が固まった
ぶんをここへ寄せる。

出すのは 4 つだけである。

1. **イベント種別の集計** — 異常系（``*_failed`` / ``*_refused`` / ``conflict``）が
   出ていないか。ゼロであることを一目で確かめる
2. **資源ごとの利用** — 何回・どれだけ持ったか。中央値と最長の開きが大きいほど、
   待ちの苦痛は少数の長時間ジョブに集中している
3. **未解放の宣言** — 取得と解放の対応が取れていないもの。今まさに走っている 1 件を
   除いて残っていれば、それは解放し忘れである
4. **申告 ETA と実績の開き** — 資源ごとにまとめて出す。案件をまたいだ集計値は
   ``rb history`` では**意図的に出していない**（機械的な補正を誘うため）が、
   ここは人間が運用を振り返る場なので別扱いとする

**本ツールは資源を知らない。** ここでも資源 ID で分岐しない。全て掲示板に載ったものを
そのまま数えるだけである。

Usage
-----
``uv run python tools/audit_report.py [--home PATH] [--since 2026-08-14]``
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

#: 正常系のイベント。これ以外が出ていれば名指しする。
NORMAL_EVENTS = frozenset(
    {"claimed", "removed", "joined", "join_removed", "wait_poll", "wait_released"}
)

#: 失敗ではないが、**誰かが資源を諦めた**という記録。健全性の問題ではないので
#: 異常系とは分けるが、頻発するなら資源が足りていない合図なので黙らせない。
NOTABLE_EVENTS = frozenset({"claim_refused"})

#: 取得と解放の組。``rb history`` と同じ対応付けを使う（主宣言は資源ごと、
#: 相乗りは資源と作業ディレクトリごと）。
OPEN_EVENTS = {"claimed": "declaration"}
CLOSE_EVENTS = {"removed": "declaration"}


def board_root(override: str | None) -> Path:
    """掲示板のルート。``rb`` と同じ規則で決める。"""
    if override:
        return Path(override)
    env = os.environ.get("RESOURCE_BROKER_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "resource-broker"
    return Path.home() / ".resource-broker"


def load(root: Path, since: str) -> list[dict]:
    """監査ログを読む。**壊れた行は飛ばす**（追記は原子的でない）。"""
    rows: list[dict] = []
    for path in sorted((root / "audit").glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict) and str(record.get("at", "")) >= since:
                rows.append(record)
    return sorted(rows, key=lambda r: str(r.get("at", "")))


def parse_at(value: object) -> datetime | None:
    """``at`` を読む。読めなければ None。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def pair_key(record: dict) -> tuple[str, str, str]:
    """対応付けの鍵。相乗りは作業ディレクトリまで見ないと取り違える。"""
    event = str(record.get("event", ""))
    kind = OPEN_EVENTS.get(event) or CLOSE_EVENTS.get(event) or "primary"
    cwd = str(record.get("cwd", "")) if kind == "join" else ""
    return (kind, str(record.get("resource", "")), cwd)


def short(resource: str) -> str:
    """表示用に ``host::`` を落とす。"""
    return resource.split("::", 1)[-1]


def duration(seconds: float) -> str:
    """秒を人が読める長さにする。"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=None, help="掲示板のルート")
    parser.add_argument("--since", default="", help="この時刻以降だけ見る（例 2026-08-14）")
    args = parser.parse_args()

    rows = load(board_root(args.home), args.since)
    if not rows:
        print("監査ログが見つかりません")
        return 0

    print(f"=== {len(rows)} 行  {rows[0].get('at')} 〜 {rows[-1].get('at')} ===\n")

    events = Counter(str(r.get("event", "?")) for r in rows)
    print("イベント")
    for name, count in events.most_common():
        if name in NORMAL_EVENTS:
            mark = ""
        elif name in NOTABLE_EVENTS:
            mark = "  <- 誰かが資源を諦めた"
        else:
            mark = "  <- 異常系"
        print(f"  {count:6}  {name}{mark}")

    # 取得と解放を突き合わせる。開いたままのものが「未解放」である。
    opened: dict[tuple[str, str, str], dict] = {}
    spans: dict[str, list[float]] = defaultdict(list)
    ratios: dict[str, list[float]] = defaultdict(list)
    for record in rows:
        event = str(record.get("event", ""))
        when = parse_at(record.get("at"))
        if when is None:
            continue
        key = pair_key(record)
        if event in OPEN_EVENTS:
            opened[key] = record
        elif event in CLOSE_EVENTS and key in opened:
            start = opened.pop(key)
            began = parse_at(start.get("at"))
            if began is None:
                continue
            elapsed = (when - began).total_seconds()
            spans[str(start.get("resource", "?"))].append(elapsed)
            eta = start.get("eta")
            due = parse_at(eta.get("at")) if isinstance(eta, dict) else None
            if due is not None:
                stated = (due - began).total_seconds()
                if stated > 0:
                    ratios[str(start.get("resource", "?"))].append(elapsed / stated)

    print("\n資源ごとの利用")
    print(f"  {'資源':<20} {'回数':>5} {'合計':>8} {'中央':>7} {'最長':>7}  申告比")
    for resource, values in sorted(spans.items(), key=lambda kv: -sum(kv[1])):
        share = ratios.get(resource) or []
        note = f"中央 {statistics.median(share):.2f} 倍（{len(share)} 件）" if share else "-"
        print(
            f"  {short(resource):<20} {len(values):>5} {duration(sum(values)):>8}"
            f" {duration(statistics.median(values)):>7} {duration(max(values)):>7}  {note}"
        )

    print("\n未解放の宣言")
    if not opened:
        print("  なし（全ての取得に解放が対応している）")
    for (kind, resource, cwd), record in opened.items():
        label = "相乗り" if kind == "join" else "主宣言"
        where = f"  cwd={cwd}" if cwd else ""
        print(f"  {label} {short(resource)}  {record.get('at')}  {record.get('job', '')}{where}")

    refused = [r for r in rows if str(r.get("event", "")) in NOTABLE_EVENTS]
    if refused:
        print("\nはじかれた記録（失敗ではない。頻発するなら資源が足りていない合図）")
        for record in refused:
            print(
                f"  {record.get('at')}  {short(str(record.get('resource', '?')))}"
                f"  保持者 {record.get('holder', '?')}"
                f"  相乗り {record.get('sharing') or '（申告なし）'}"
            )

    strange = [
        r
        for r in rows
        if str(r.get("event", "")) not in NORMAL_EVENTS
        and str(r.get("event", "")) not in NOTABLE_EVENTS
    ]
    print("\n異常系イベント")
    if not strange:
        print("  なし")
    for record in strange:
        print(f"  {record.get('at')}  {json.dumps(record, ensure_ascii=False)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
