"""コマンドラインインタフェース（``resource-broker`` / ``rb``）。

Phase 1 で提供するのは ``status`` / ``claim`` / ``release`` の 3 つ。
ラッパー（``run``）とフックは Phase 2 以降で追加する。

**本ツールは資源を調べない。** 調べるのはセッション（Claude Code）の仕事であり、
本ツールがやるのは「調べたことを申告させ、掲示板に残し、他セッションから見えるようにする」
ことだけである（DESIGN.md「調べるのは誰か」）。したがって ``claim`` は
``--observed`` を必須とする。

**終了コードの方針**: 本ツール自身の内部エラーでは 0 を返す（fail-open）。
1 を返すのは「掲示板が正常に読めた上で、使用中だと判定できた」場合だけである。
インフラの故障と資源の競合を混同しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import clock, liveness, naming, platform_info
from .board import Board, Entry, build_entry
from .liveness import Observation, Verdict

EXIT_OK = 0
EXIT_BUSY = 1

#: ``--found`` の受け付ける値と、それが表す実測の結論。
FOUND_CHOICES: dict[str, bool | None] = {"busy": True, "free": False, "unknown": None}


def assess(
    board: Board, resource_id: str, observation: Observation | None = None
) -> tuple[Verdict, Entry | None]:
    """資源の状態を判定する。掲示板・OS 情報・申告された実測を liveness に渡す。

    Parameters
    ----------
    board : Board
        掲示板。
    resource_id : str
        正規化済みの資源 ID。
    observation : Observation, optional
        セッションが調べた結果。省略時は「調べていない」として扱う。
        **本関数は資源を調べない**。資源の種別で分岐する箇所はここに存在しない。
    """
    entry = board.read(resource_id)
    verdict = liveness.judge(
        has_entry=entry is not None,
        since=entry.since_dt if entry else None,
        boot=platform_info.boot_time(),
        observation=observation or Observation(),
        pid_alive=platform_info.pid_alive(entry.pid) if entry else None,
        now=clock.now(),
    )
    return verdict, entry


def _cmd_status(args: argparse.Namespace) -> int:
    board = Board(args.home)
    targets = (
        [naming.normalize(r) for r in args.resource]
        if args.resource
        else [entry.resource for entry in board.list_all()]
    )

    rows = []
    for resource_id in targets:
        verdict, entry = assess(board, resource_id)
        rows.append(
            {
                "resource": resource_id,
                "display": (entry.display if entry else "") or naming.display_default(resource_id),
                "verdict": str(verdict),
                "reason": liveness.explain(verdict),
                "free": liveness.is_free(verdict),
                "holder": entry.holder if entry else None,
                "since": entry.since if entry else None,
                "log": entry.log if entry else None,
                "observed": entry.observed if entry else None,
            }
        )

    if args.json:
        print(json.dumps({"resources": rows}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not rows:
        print("掲示板は空です（誰も資源を宣言していません）")
        print("使う前に自分で資源の状態を調べ、rb claim で宣言すること")
        return EXIT_OK

    for row in rows:
        mark = "空き" if row["free"] else "使用中"
        print(f"{row['display']:<24} {mark:<6} {row['reason']}")
        holder = row["holder"] or {}
        if holder:
            job = holder.get("job") or "(ジョブ未記入)"
            print(f"{'':<24} 宣言   {holder.get('session', '?')} / {job}")
            print(f"{'':<24} since  {row['since']}")
        if row["log"]:
            print(f"{'':<24} log    {row['log']}")
        observed = row["observed"] or {}
        if observed.get("note"):
            print(f"{'':<24} 観測   {observed['note']}")
            print(f"{'':<24}        （{observed.get('at', '時刻不明')} 時点の申告）")
    return EXIT_OK


def _cmd_claim(args: argparse.Namespace) -> int:
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)
    verdict, entry = assess(board, resource_id, observation)

    # 自分で調べて使用中だったのなら、掲示板に何が書いてあろうと宣言してはならない。
    if observation.busy is True:
        print("自分の調査で使用中と分かっているため宣言できません", file=sys.stderr)
        print(f"  観測  : {observation.note}", file=sys.stderr)
        return EXIT_BUSY

    if entry is not None and not liveness.is_free(verdict) and not args.force:
        print(f"使用中のため宣言できません: {liveness.explain(verdict)}", file=sys.stderr)
        print(f"  宣言者: {entry.session} / {entry.job}", file=sys.stderr)
        print(f"  since : {entry.since}", file=sys.stderr)
        if entry.log:
            print(f"  log   : {entry.log}", file=sys.stderr)
        return EXIT_BUSY

    if entry is not None:
        reason = "強制取得" if args.force else f"幽霊と判定した（{liveness.explain(verdict)}）"
        board.remove(resource_id, reason=reason)

    new_entry = build_entry(
        resource_id,
        job=args.job,
        display=args.display or "",
        log=args.log,
        observed={"note": observation.note, "found": args.found},
    )
    if not board.try_claim(new_entry):
        # ここに来るのは、判定してから作成するまでの間に他セッションが取った場合。
        print("他のセッションが先に宣言しました", file=sys.stderr)
        return EXIT_BUSY

    print(f"宣言しました: {new_entry.display} / {new_entry.job}")
    return EXIT_OK


def _cmd_release(args: argparse.Namespace) -> int:
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    if board.remove(resource_id, reason="release コマンド"):
        print(f"解放しました: {naming.display_default(resource_id)}")
    else:
        print("宣言は見つかりませんでした（既に解放済みです）")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """引数パーサを組み立てる。"""
    parser = argparse.ArgumentParser(
        prog="resource-broker",
        description="並行する Claude Code セッション間で有限資源の使用状況を共有する掲示板",
        epilog=(
            "本ツールは資源を調べない。調べるのは資源を使おうとするセッションの仕事であり、"
            "claim はその結果の申告を必須とする。"
        ),
    )
    parser.add_argument("--home", default=None, help="掲示板のルート（既定は環境依存）")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="資源の状態を表示する")
    status.add_argument("resource", nargs="*", help="対象の資源 ID（省略時は宣言のある全件）")
    status.add_argument("--json", action="store_true", help="JSON で出力する")
    status.set_defaults(func=_cmd_status)

    claim = sub.add_parser(
        "claim",
        help="資源を宣言する（先に自分で調べること）",
        description=(
            "資源を宣言する。--observed には「自分が何を見たか」を書く。"
            "本ツールは中身を解釈せず、観測点として掲示板に残すだけである。"
        ),
    )
    claim.add_argument("resource", help="資源 ID")
    claim.add_argument("--job", required=True, help="何をするか（1 行）")
    claim.add_argument(
        "--observed",
        required=True,
        help="自分で調べて何を見たか（例: 'nvidia-smi: compute apps なし'）",
    )
    claim.add_argument(
        "--found",
        choices=sorted(FOUND_CHOICES),
        default="unknown",
        help="調べた結論。既定は unknown（分からなかった）",
    )
    claim.add_argument("--log", default=None, help="進捗が読めるログのパス")
    claim.add_argument("--display", default=None, help="表示名")
    claim.add_argument("--force", action="store_true", help="他者の宣言を退けて強制的に取得する")
    claim.set_defaults(func=_cmd_claim)

    release = sub.add_parser("release", help="宣言を解放する")
    release.add_argument("resource", help="資源 ID")
    release.set_defaults(func=_cmd_release)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """エントリポイント。

    内部エラーは 0 を返して通す（fail-open）。本ツールの不具合で
    ユーザーの作業を止めないことを、コード上でも保証する。
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help や引数不備。argparse の意図どおりに返す
        return int(exc.code or 0)

    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - fail-open
        print(f"[resource-broker] 内部エラーのため判定を省略します: {exc}", file=sys.stderr)
        try:
            Board(args.home).audit("cli_internal_error", command=args.command, error=str(exc))
        except Exception:  # noqa: BLE001
            pass
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
