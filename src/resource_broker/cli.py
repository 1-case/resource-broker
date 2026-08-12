"""コマンドラインインタフェース（``resource-broker`` / ``rb``）。

提供するのは ``status`` / ``claim`` / ``release``（Phase 1）と
ラッパー ``run``（Phase 2）。フックは Phase 3 で追加する。

**本ツールは資源を調べない。** 調べるのはセッション（Claude Code）の仕事であり、
本ツールがやるのは「調べたことを申告させ、掲示板に残し、他セッションから見えるようにする」
ことだけである（DESIGN.md「Who Investigates」）。したがって ``claim`` は
``--observed`` を必須とする。

**終了コードの方針**: 本ツール自身の内部エラーでは 0 を返す（fail-open）。
1 を返すのは「掲示板が正常に読めた上で、使用中だと判定できた」場合だけである。
インフラの故障と資源の競合を混同しない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import clock, liveness, naming, platform_info, runner, waiting
from .board import Board, Entry, build_entry
from .liveness import Observation, Verdict

EXIT_OK = 0
EXIT_BUSY = 1

#: 引数の不備。argparse と同じ値にそろえる。
EXIT_USAGE = 2

#: Ctrl+C で中断されたときの終了コード（シェルの慣習に合わせる）。
EXIT_INTERRUPTED = 130

#: ``--found`` の受け付ける値と、それが表す実測の結論。
FOUND_CHOICES: dict[str, bool | None] = {"busy": True, "free": False, "unknown": None}

#: 子プロセスの起動。テストで差し替える（実プロセスを起動しないため）。
SPAWN: runner.Spawn = runner.default_spawn


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
                "eta": entry.eta if entry else None,
                "usage": entry.usage if entry else None,
                "sharing": (entry.sharing or None) if entry else None,
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
        eta = row["eta"] or {}
        if eta.get("stated"):
            at = f"（{eta['at']} 頃）" if eta.get("at") else ""
            print(f"{'':<24} ETA    {eta['stated']}{at}  ※申告であって約束ではない")
        usage = row["usage"] or {}
        if usage.get("peak") or usage.get("avg"):
            print(
                f"{'':<24} 見積   瞬時最大 {usage.get('peak') or '-'}"
                f" / 平均 {usage.get('avg') or '-'}"
            )
        if row["sharing"]:
            print(f"{'':<24} 相乗り {row['sharing']}")
        if row["log"]:
            print(f"{'':<24} log    {row['log']}")
        observed = row["observed"] or {}
        if observed.get("note"):
            print(f"{'':<24} 観測   {observed['note']}")
            print(f"{'':<24}        （{observed.get('at', '時刻不明')} 時点の申告）")
    return EXIT_OK


def acquire(
    board: Board,
    resource_id: str,
    observation: Observation,
    *,
    job: str,
    found: str,
    eta: str = "",
    peak: str = "",
    avg: str = "",
    sharing: str = "",
    display: str = "",
    log: str | None = None,
    force: bool = False,
    pid: int | None = None,
) -> tuple[Entry | None, int]:
    """宣言を取得する。``claim`` と ``run`` で共通の判断である。

    Parameters
    ----------
    pid : int, optional
        宣言者として記録する PID。**手動の ``claim`` では渡さない**。
        渡してよいのはラッパー（``run``）だけで、そこではラッパー自身が
        ジョブと同じ寿命を持つため生存確認が意味を持つ。

    Returns
    -------
    tuple of (Entry or None, int)
        取得できたエントリと終了コード。取得できなければ ``(None, EXIT_BUSY)``。
    """
    verdict, entry = assess(board, resource_id, observation)

    # 自分で調べて使用中だったのなら、掲示板に何が書いてあろうと宣言してはならない。
    if observation.busy is True:
        print("自分の調査で使用中と分かっているため宣言できません", file=sys.stderr)
        print(f"  観測  : {observation.note}", file=sys.stderr)
        return None, EXIT_BUSY

    if entry is not None and not liveness.is_free(verdict) and not force:
        print(f"使用中のため宣言できません: {liveness.explain(verdict)}", file=sys.stderr)
        print(f"  宣言者: {entry.session} / {entry.job}", file=sys.stderr)
        print(f"  since : {entry.since}", file=sys.stderr)
        if entry.log:
            print(f"  log   : {entry.log}", file=sys.stderr)
        return None, EXIT_BUSY

    if entry is not None:
        reason = "強制取得" if force else f"幽霊と判定した（{liveness.explain(verdict)}）"
        board.remove(resource_id, reason=reason)

    new_entry = build_entry(
        resource_id,
        job=job,
        display=display,
        log=log,
        pid=pid,
        observed={"note": observation.note, "found": found},
        eta=eta,
        peak=peak,
        avg=avg,
        sharing=sharing,
    )
    if not board.try_claim(new_entry):
        # ここに来るのは、判定してから作成するまでの間に他セッションが取った場合。
        print("他のセッションが先に宣言しました", file=sys.stderr)
        return None, EXIT_BUSY

    return new_entry, EXIT_OK


def _cmd_claim(args: argparse.Namespace) -> int:
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)

    # 手動の claim では PID を記録しない（CLI プロセスは即座に終了するため）。
    entry, code = acquire(
        board,
        resource_id,
        observation,
        job=args.job,
        found=args.found,
        eta=args.eta,
        peak=args.peak or "",
        avg=args.avg or "",
        sharing=args.sharing or "",
        display=args.display or "",
        log=args.log,
        force=args.force,
    )
    if entry is None:
        return code

    print(f"宣言しました: {entry.display} / {entry.job}")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    """資源を宣言してからコマンドを実行し、終わったら必ず解放する。

    ``finally`` で解放するため、子プロセスが異常終了しても、例外が飛んでも、
    Ctrl+C で中断しても宣言は残らない。**ラッパーごと強制終了された場合だけ**
    エントリが残るが、そこは PID を記録してあるため幽霊判定が拾える。

    終了コードは**子プロセスのものをそのまま返す**。資源を取得できずに
    実行しなかった場合だけ ``EXIT_BUSY`` を返し、その旨を stderr に出す。
    """
    if not args.trailing:
        print("実行するコマンドを `--` の後ろに指定してください", file=sys.stderr)
        print(
            '  例: rb run --res GPU0 --job "学習" --observed "..." -- python train.py',
            file=sys.stderr,
        )
        return EXIT_USAGE

    board = Board(args.home)
    resource_id = naming.normalize(args.res)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)
    log_path = Path(args.log) if args.log else runner.build_log_path(board.root, resource_id)

    # ラッパーはジョブと同じ寿命を持つ。ここでだけ PID を記録する。
    entry, code = acquire(
        board,
        resource_id,
        observation,
        job=args.job,
        found=args.found,
        eta=args.eta,
        peak=args.peak or "",
        avg=args.avg or "",
        sharing=args.sharing or "",
        display=args.display or "",
        log=str(log_path),
        force=args.force,
        pid=os.getpid(),
    )
    if entry is None:
        print("資源を取得できなかったため、コマンドを実行していません", file=sys.stderr)
        return code

    print(f"宣言しました: {entry.display} / {entry.job}")
    print(f"ログ: {log_path}")
    try:
        return runner.execute(list(args.trailing), log_path, spawn=SPAWN)
    except KeyboardInterrupt:
        print("中断されました", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        board.remove(resource_id, reason="rb run の終了")
        print(f"解放しました: {entry.display}")


def _cmd_wait(args: argparse.Namespace) -> int:
    """資源が解放されるまで待つ。

    ETA では打ち切らない。掲示板の ETA は申告であって約束ではないため、
    過ぎたからといって待機をやめる根拠にはしない（CLAUDE.md「Time Handling」）。
    打ち切るのは呼び出し側が指定した ``--timeout`` だけである。
    """
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)

    entry = board.read(resource_id)
    if entry is None:
        print(f"既に解放されています: {naming.display_default(resource_id)}")
        return EXIT_OK

    print(f"待機します: {entry.display} <- {entry.session} / {entry.job}")
    if entry.eta:
        stated = entry.eta.get("stated") if isinstance(entry.eta, dict) else None
        at = entry.eta.get("at") if isinstance(entry.eta, dict) else None
        print(f"  ETA   {stated}{f'（{at} 頃）' if at else ''}  ※申告であって約束ではない")
    if entry.log:
        print(f"  log   {entry.log}")
    print(f"  {args.interval:g} 秒ごとに確認、上限 {args.timeout:g} 秒。Ctrl+C で中断できます")

    try:
        result = waiting.wait_for_release(
            board, resource_id, interval_s=args.interval, timeout_s=args.timeout
        )
    except KeyboardInterrupt:
        print("中断しました（宣言はそのままです）", file=sys.stderr)
        return EXIT_INTERRUPTED

    if result.released:
        print(f"解放されました（{result.polls} 回確認 / {result.waited_s:.0f} 秒）")
        print("使う前にもう一度自分で状態を調べること（解放＝空きとは限らない）")
        return EXIT_OK

    print(
        f"上限に達しました（{result.polls} 回確認 / {result.waited_s:.0f} 秒）。まだ使用中です",
        file=sys.stderr,
    )
    return EXIT_BUSY


def _cmd_history(args: argparse.Namespace) -> int:
    """過去の宣言を振り返る。見積もりの精度を上げるための材料である。"""
    board = Board(args.home)
    target = naming.normalize(args.resource) if args.resource else None

    records = []
    try:
        paths = sorted(board.audit_dir.glob("*.jsonl"))
    except OSError:
        paths = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("event") != "claimed":
                continue
            if target and record.get("resource") != target:
                continue
            records.append(record)

    records = records[-args.limit :]

    if args.json:
        print(json.dumps({"claims": records}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not records:
        print("過去の宣言は見つかりませんでした")
        return EXIT_OK

    for record in records:
        eta = record.get("eta") or {}
        usage = record.get("usage") or {}
        eta_text = eta.get("stated") if isinstance(eta, dict) else None
        peak = usage.get("peak") if isinstance(usage, dict) else None
        avg = usage.get("avg") if isinstance(usage, dict) else None
        print(
            f"{record.get('at', '?')}  {naming.display_default(str(record.get('resource', '?')))}"
        )
        print(f"    job  {record.get('job', '')}")
        if eta_text:
            print(f"    ETA  {eta_text}")
        if peak or avg:
            print(f"    見積 peak={peak or '-'} avg={avg or '-'}")
    print()
    print("前回の見積もりと実績を突き合わせ、次の申告の精度を上げること")
    return EXIT_OK


def _cmd_release(args: argparse.Namespace) -> int:
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    if board.remove(resource_id, reason="release コマンド"):
        print(f"解放しました: {naming.display_default(resource_id)}")
    else:
        print("宣言は見つかりませんでした（既に解放済みです）")
    return EXIT_OK


def _add_declaration_options(parser: argparse.ArgumentParser) -> None:
    """宣言に必要なオプションを付ける。``claim`` と ``run`` で共通である。

    必須は ``--job`` ``--observed`` ``--eta`` の 3 つ。ここが本ツールの強制であり、
    **書かせること自体に意味がある**。ETA を必須にしているのは、正確な値が欲しいからではなく、
    「どれくらいで終わるか」を一度考えさせるためである。外れても本ツールは何も判断しない。
    """
    parser.add_argument("--job", required=True, help="何をするか（1 行）")
    parser.add_argument(
        "--observed",
        required=True,
        help="自分で調べて何を見たか（例: 'nvidia-smi: compute apps なし'）",
    )
    parser.add_argument(
        "--eta",
        required=True,
        help=(
            "終わるまでの見込み。'30m' '2h' '1h30m' なら絶対時刻を機械が計算して併記する。"
            "自由記述も可（'モデル次第' 等）。**判断には使わない**"
        ),
    )
    parser.add_argument(
        "--found",
        choices=sorted(FOUND_CHOICES),
        default="unknown",
        help="調べた結論。既定は unknown（分からなかった）",
    )
    parser.add_argument(
        "--peak", default=None, help="利用見積もりの瞬時最大（例: 'VRAM 6GB' '80%%' '4 cores'）"
    )
    parser.add_argument("--avg", default=None, help="利用見積もりの平均（同上）")
    parser.add_argument(
        "--sharing",
        default=None,
        help="相乗りの可否と条件（例: '可（VRAM 残 6GB まで）' '不可'）。本ツールは解釈しない",
    )
    parser.add_argument("--log", default=None, help="進捗が読めるログのパス")
    parser.add_argument("--display", default=None, help="表示名")
    parser.add_argument("--force", action="store_true", help="他者の宣言を退けて強制的に取得する")


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
    _add_declaration_options(claim)
    claim.set_defaults(func=_cmd_claim)

    release = sub.add_parser("release", help="宣言を解放する")
    release.add_argument("resource", help="資源 ID")
    release.set_defaults(func=_cmd_release)

    run = sub.add_parser(
        "run",
        help="資源を宣言してコマンドを実行し、終了時に必ず解放する",
        description=(
            "宣言・ログ出力・解放を機械的に行う。解放は finally で行うため、"
            "異常終了でも中断でもエントリは残らない。"
            "終了コードは子プロセスのものをそのまま返す。"
        ),
    )
    run.add_argument("--res", required=True, help="資源 ID")
    _add_declaration_options(run)
    run.set_defaults(func=_cmd_run)

    wait = sub.add_parser(
        "wait",
        help="資源が解放されるまで待つ",
        description=(
            "掲示板から宣言が消えるまでブロックする。ETA では打ち切らない"
            "（申告であって約束ではない）。打ち切るのは --timeout だけである。"
            "毎回のポーリングは監査ログに残る。"
        ),
    )
    wait.add_argument("resource", help="資源 ID")
    wait.add_argument(
        "--interval",
        type=float,
        default=waiting.DEFAULT_INTERVAL_S,
        help=f"ポーリング間隔の秒数（既定 {waiting.DEFAULT_INTERVAL_S:g}）",
    )
    wait.add_argument(
        "--timeout",
        type=float,
        default=waiting.DEFAULT_TIMEOUT_S,
        help=f"待機の上限秒数（既定 {waiting.DEFAULT_TIMEOUT_S:g}）。超えたら一度戻る",
    )
    wait.set_defaults(func=_cmd_wait)

    history = sub.add_parser(
        "history",
        help="過去の宣言を振り返る（見積もりの根拠にする）",
        description=(
            "監査ログから過去の宣言と解放を拾う。前回どう見積もって実際どうだったかを"
            "見返すためのもので、見積もりの精度を回ごとに上げるために使う。"
        ),
    )
    history.add_argument("resource", nargs="?", default=None, help="資源 ID（省略時は全件）")
    history.add_argument("--limit", type=int, default=20, help="表示する件数（既定 20）")
    history.add_argument("--json", action="store_true", help="JSON で出力する")
    history.set_defaults(func=_cmd_history)

    return parser


def split_trailing(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """``--`` の前後で引数を分ける。

    外部コマンドを引数に取る ``run`` のために、argparse へ渡す前に切り離す。
    argparse に解釈させると、実行したいコマンド側の ``--epochs 10`` のような
    引数を本ツールのオプションと取り違える。

    Returns
    -------
    tuple of (list of str, list of str)
        ``--`` より前と後。``--`` が無ければ後ろは空リスト。
    """
    argv = list(argv)
    if "--" not in argv:
        return argv, []
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def main(argv: Sequence[str] | None = None) -> int:
    """エントリポイント。

    内部エラーは 0 を返して通す（fail-open）。本ツールの不具合で
    ユーザーの作業を止めないことを、コード上でも保証する。
    """
    head, trailing = split_trailing(sys.argv[1:] if argv is None else argv)

    parser = build_parser()
    try:
        args = parser.parse_args(head)
    except SystemExit as exc:  # --help や引数不備。argparse の意図どおりに返す
        return int(exc.code or 0)
    args.trailing = trailing

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
