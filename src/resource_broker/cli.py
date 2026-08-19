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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import clock, liveness, naming, platform_info, runner, waiting
from .board import (
    Board,
    Entry,
    JoinResult,
    LockState,
    RemovalResult,
    UpdateResult,
    build_entry,
    build_eta,
)
from .liveness import Observation, Verdict

EXIT_OK = 0
EXIT_BUSY = 1

#: 引数の不備。argparse と同じ値にそろえる。
EXIT_USAGE = 2

#: Ctrl+C で中断されたときの終了コード（シェルの慣習に合わせる）。
EXIT_INTERRUPTED = 130

#: ``wait`` が内部エラーで待てなかったときの終了コード。
#:
#: **上限到達（``EXIT_BUSY``）と分ける。** どちらも 1 にすると、呼び出し側が
#: 「上限まで待った」と「1 度も待っていない」を区別できない。対処が違う。
EXIT_WAIT_BROKEN = 3

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


def _known_resources(board: Board) -> list[str]:
    """掲示板に載っている資源を列挙する。"""
    return _known_resources_detailed(board)[0]


def _known_resources_detailed(board: Board) -> tuple[list[str], bool]:
    """掲示板に載っている資源と、**読めなかったものがあったか**を返す。

    主宣言だけでなく**相乗りだけが残っている資源**も拾う。主宣言が先に解放されて
    相乗りが残る場合があり、そこを取りこぼすと「誰も使っていない」に見えてしまう。

    読めなかったことを畳まない。畳むと「掲示板は空です」と断定してしまい、
    **実際には使われている資源を空きとして配る**（DESIGN.md「Liveness Judgment」の
    非対称性の裏返しであり、断定してよい側ではない）。
    """
    entries, unreadable = board.list_all_detailed()
    resources = [entry.resource for entry in entries]
    seen = set(resources)
    joins, joins_unreadable = board.list_all_joins_detailed()
    for join in joins:
        if join.resource not in seen:
            seen.add(join.resource)
            resources.append(join.resource)
    return resources, unreadable or joins_unreadable


@dataclass(frozen=True)
class Acquisition:
    """取得の結果。

    Attributes
    ----------
    entry : Entry or None
        取得した宣言。取得できなかったときは None。
    code : int
        終了コード。
    declared : bool
        掲示板に宣言を**残せた**か。掲示板が書けなかった場合、fail-open として作業は
        通すが宣言は残っていない。これを区別しないと「宣言しました」という嘘を出す。
    """

    entry: Entry | None
    code: int
    declared: bool = False


def _describe(entry: Entry) -> dict[str, object]:
    """1 つの宣言を機械可読な形にする。相乗りも同じ形で並べる。"""
    return {
        "holder": entry.holder,
        "job": entry.job,
        "since": entry.since,
        "log": entry.log,
        "observed": entry.observed,
        "eta": entry.eta,
        "usage": entry.usage,
        "sharing": entry.sharing or None,
    }


def _cmd_status(args: argparse.Namespace) -> int:
    board = Board(args.home)
    if args.resource:
        targets, unreadable = [naming.normalize(r) for r in args.resource], False
    else:
        targets, unreadable = _known_resources_detailed(board)

    rows = []
    for resource_id in targets:
        verdict, entry = assess(board, resource_id)
        joins = board.list_joins(resource_id)
        # **「空き」と「誰も使っていない」は別の問いである。** 2 つに分けて両方載せる。
        #
        # free     : 主宣言の枠が取れるか。相乗りは影響しない（相乗りがあっても
        #            主宣言は取れる）。`claim` の可否と一致する
        # occupied : 誰か 1 人でも宣言しているか（主宣言または相乗り）。表示と
        #            フックの絞り込みはこちらを使う。相乗りだけが残った資源を
        #            「空き」と出すと、実際に使っている者がいるのに通知から消える
        free = liveness.is_free(verdict)
        occupied = (not free) or bool(joins)
        rows.append(
            {
                "joins": [_describe(join) for join in joins],
                "holders": (1 if entry else 0) + len(joins),
                "has_primary": entry is not None,
                "resource": resource_id,
                "display": (entry.display if entry else "") or naming.display_default(resource_id),
                # 一覧の見出し。**資源 ID を必ず含める**（display による置き換えを許さない）。
                "label": naming.label(resource_id, entry.display if entry else ""),
                "verdict": str(verdict),
                "reason": (
                    liveness.explain(verdict)
                    if entry is not None
                    else f"主宣言は無いが相乗りが {len(joins)} 件ある"
                )
                if occupied
                else liveness.explain(verdict),
                "free": free,
                "occupied": occupied,
                "holder": entry.holder if entry else None,
                "since": entry.since if entry else None,
                # 宣言してからの経過。**表示のためだけの値**であり、判断には使わない。
                # 長く持っていることは幽霊である証拠にならない。
                "held_for_seconds": _held_seconds(entry) if entry else None,
                "log": entry.log if entry else None,
                "observed": entry.observed if entry else None,
                "eta": entry.eta if entry else None,
                "usage": entry.usage if entry else None,
                "sharing": (entry.sharing or None) if entry else None,
            }
        )

    if args.json:
        # ``partial`` は「読めなかった宣言がある」。**空と読ませない**ための旗である。
        print(json.dumps({"resources": rows, "partial": unreadable}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not rows:
        if unreadable:
            print("掲示板を読めませんでした。**空とは限りません**")
            print(f"  掲示板の場所: {board.root}")
            print("  権限・パス・ネットワークドライブの接続を確かめること")
            return EXIT_OK
        print("掲示板は空です（誰も資源を宣言していません）")
        print("使う前に自分で資源の状態を調べ、rb claim で宣言すること")
        return EXIT_OK

    if unreadable:
        print("注意: 掲示板の一部を読めませんでした。**これで全部とは限りません**")

    for row in rows:
        # 表示は occupied（誰か 1 人でも宣言しているか）で決める。
        mark = "使用中" if row["occupied"] else "空き"
        print(f"{row['label']:<24} {mark:<6} {row['reason']}")
        holder = row["holder"] or {}
        if holder:
            job = holder.get("job") or "(ジョブ未記入)"
            print(f"{'':<24} 宣言   {holder.get('session', '?')} / {job}")
            held = row["held_for_seconds"]
            elapsed = f"（{_format_duration(held)} 経過）" if held is not None else ""
            print(f"{'':<24} since  {row['since']}{elapsed}")
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

        for index, join in enumerate(row["joins"], start=1):
            holder = join["holder"] or {}
            # **PID を出す。** 相乗りの自動回収は無い（退去の根拠を 3 つから増やさない）
            # ので、死んだ申告を見分けて `rb release --join` を選ぶのは読む側の仕事に
            # なる。記録してあるのに読めない場所に置いたままでは、記録した意味が無い。
            pid = holder.get("pid")
            marker = f" (pid {pid})" if isinstance(pid, int) and not isinstance(pid, bool) else ""
            print(f"{'':<24} 相乗り{index} {holder.get('session', '?')}{marker} / {join['job']}")
            print(f"{'':<24}        since {join['since']}")
            join_eta = join["eta"] or {}
            if join_eta.get("stated"):
                print(f"{'':<24}        ETA {join_eta['stated']}")
            join_usage = join["usage"] or {}
            if join_usage.get("peak") or join_usage.get("avg"):
                print(
                    f"{'':<24}        見積 瞬時最大 {join_usage.get('peak') or '-'}"
                    f" / 平均 {join_usage.get('avg') or '-'}"
                )
            if join["log"]:
                print(f"{'':<24}        log {join['log']}")

        if row["holders"] > 1 or (row["joins"] and not row["has_primary"]):
            primary = 1 if row["has_primary"] else 0
            print(
                f"{'':<24} 合計   {row['holders']} 件の宣言"
                f"（主 {primary} + 相乗り {len(row['joins'])}）"
            )
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
) -> Acquisition:
    """宣言を取得する。``claim`` と ``run`` で共通の判断である。

    Parameters
    ----------
    pid : int, optional
        宣言者として記録する PID。**手動の ``claim`` では渡さない**。
        渡してよいのはラッパー（``run``）だけで、そこではラッパー自身が
        ジョブと同じ寿命を持つため生存確認が意味を持つ。

    Returns
    -------
    Acquisition
        取得できたエントリ・終了コード・掲示板に残せたか。
    """
    # 自分で調べて使用中だったのなら、掲示板を読むまでもなく宣言してはならない。
    if observation.busy is True:
        print("自分の調査で使用中と分かっているため宣言できません", file=sys.stderr)
        print(f"  観測  : {observation.note}", file=sys.stderr)
        return Acquisition(None, EXIT_BUSY)

    # **排他区間の中で I/O をしない。** ここで stderr へ直接書くと、パイプが詰まったときに
    # ロックを 30 秒以上保持し、他セッションに steal される。文言は溜めて区間の外で出す。
    notices: list[str] = []
    # 監査も同じ理由で溜める。ファイルへの追記は区間の外で行う。
    deferred_audit: list[tuple[str, dict[str, object]]] = []

    def under_lock(lock: LockState) -> Acquisition:
        """ロックを保持したまま行う判断。**正しさはロックではなく CAS に乗る。**

        **ロックが取れないことを理由に諦めない。** どちらの取れなさも、資源が使用中で
        あるという証拠を 1 バイトも含んでいない（掲示板をまだ読んでいない）。ここで
        ``EXIT_BUSY`` を返すと 2 つの嘘をつくことになる。他セッションが ``release``
        の最中でもロックは握られるので**資源が今まさに空こうとしている瞬間に「使用中」**
        と答え、ロックを持ったままプロセスが死ねば ``LOCK_STALE_S`` のあいだ
        **全セッションの取得が「使用中」で止まる**。本ツールの故障を資源の競合として
        報告することは CLAUDE.md「Fail-Open」が名指しで禁じている。

        続行しても安全性は落ちない。正しさは nonce の CAS と ``try_claim`` の
        ``O_EXCL`` が担保しており、本当に競っていれば ``try_claim`` が負けて
        **真の busy** が返る。
        """
        if lock is LockState.CONTENDED:
            notices.append(
                "[rb] 他のセッションが同じ資源を操作中です。排他を弱めて続行します"
                "（監査ログを参照）"
            )
        elif lock is LockState.UNAVAILABLE:
            notices.append("[rb] ロックが使えないため排他を弱めて続行します（監査ログを参照）")

        verdict, entry = assess(board, resource_id, observation)

        if entry is not None and not liveness.is_free(verdict) and not force:
            notices.append(f"使用中のため宣言できません: {liveness.explain(verdict)}")
            notices.append(f"  宣言者: {entry.session} / {entry.job}")
            notices.append(f"  since : {entry.since}")
            # **保持者が立てた旗をそのまま見せる。中身は解釈しない。**
            # ここを出さないと、保持者が「相乗り可」と書いていても、はじかれた側には
            # 「使用中」しか見えない。実際にあるセッションが相乗り可の GPU を諦めて CPU へ
            # 逃げた（掲示板には可と書いてあった）。掲示板が持っている材料を
            # **判断が必要なその場で**出せていなければ、載せていないのと同じである。
            if entry.sharing:
                notices.append(f"  相乗り: {entry.sharing}  ※保持者の申告")
            if entry.log:
                notices.append(f"  log   : {entry.log}")
            # 可否は当事者が決める（本ツールは判断しない）。**次の一手を必ず示す。**
            # 分岐しているのは「旗が立っているか」だけで、旗の中身では分岐しない。
            hint = naming.display_default(resource_id)
            notices.append(
                f"  → 相乗りするなら rb join --res {hint} ...、空くまで待つなら rb wait {hint}"
            )
            deferred_audit.append(
                (
                    "claim_refused",
                    {
                        "resource": resource_id,
                        "verdict": str(verdict),
                        "holder": entry.session,
                        "sharing": entry.sharing or "",
                    },
                )
            )
            return Acquisition(None, EXIT_BUSY)

        # **相乗りがいることは知らせるが、止めない。** 主宣言の枠が空いていることと、
        # 誰も資源を使っていないことは別である。相乗りしてよいかは当事者が決める
        # （CLAUDE.md「Resource Agnosticism」/ DESIGN.md「Sharing」）。
        joins = board.list_joins(resource_id)
        if joins:
            notices.append(
                f"[rb] この資源には相乗りが {len(joins)} 件あります"
                "（主宣言の枠は空いています。可否は当事者で決めること）"
            )
            notices.extend(f"  相乗り: {join.session} / {join.job}" for join in joins)

        if entry is not None:
            # **読んだ宣言と一致するときだけ消す（CAS）。** 無条件に消すと、ロックが
            # 外れている間に A と B が同じ幽霊を見たとき、A が取り直した直後に B が
            # **A の生きた宣言を消して**両方とも取得に成功する。二重取得そのものである。
            reason = "強制取得" if force else f"幽霊と判定した（{liveness.explain(verdict)}）"
            removal = board.remove_if_nonce(resource_id, expect_nonce=entry.nonce, reason=reason)
            if removal not in (RemovalResult.REMOVED, RemovalResult.ABSENT):
                # ABSENT は「読んだ直後に誰かが消した」であり、枠は空いている。
                # 先着は次の try_claim（O_EXCL）が決めるのでそのまま進んでよい。
                notices.append(_explain_failed_displacement(removal))
                return Acquisition(None, EXIT_BUSY)

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
            # 誰かが先に取ったのか、掲示板が書けないのかを見分ける。
            current = board.read(resource_id)
            if current is not None:
                notices.append("ほぼ同時に他セッションが宣言しました")
                notices.append(f"  宣言者: {current.session} / {current.job}")
                return Acquisition(None, EXIT_BUSY)
            if board.path_for(resource_id).exists():
                # ファイルはあるのに読めない = 壊れたエントリが居座っている。放っておくと
                # status に出ず、wait は即座に解放と答え、claim は永久に失敗する。
                # **人間がファイルを手で消すしか回復手段が無い状態を作らない。**
                notices.append(
                    "[rb] 壊れたエントリが居座っています（読めないファイルが掲示板にあります）"
                )
                notices.append(
                    f"  掃除: rb release {resource_id} --force（そのあと改めて claim すること）"
                )
                return Acquisition(new_entry, EXIT_OK, declared=False)
            # 掲示板に書けない。**これは資源の競合ではない**ので通す（fail-open）。
            # ただし宣言は残っていないので、呼び出し側が嘘を言わないよう declared=False。
            notices.append(
                "[rb] 宣言を掲示板に残せませんでした（監査ログを参照）。作業は止めません"
            )
            return Acquisition(new_entry, EXIT_OK, declared=False)

        return Acquisition(new_entry, EXIT_OK, declared=True)

    # **溜めた説明は例外が飛んでも出す。** 途中で落ちたときこそ「相乗りが N 件ある」
    # 「排他を弱めて続行した」は読む側に必要な材料であり、そこで消えると
    # 何も起きなかったように見える。
    try:
        with board.locked(resource_id) as lock:
            result = under_lock(lock)
    finally:
        # **掲示板へ書いた後なので、ここで例外を出さない**（`_say` の docstring 参照）。
        for line in notices:
            _say(line, err=True)
        # **はじいたことを必ず残す。** 記録が無いと「誰が GPU を諦めたか」を
        # 後から追えない（実際に追えなかった）。判定したのに黙るのは、監視が
        # 死んだのと区別が付かない（CLAUDE.md「Silence Is Not Success」）。
        # Board.audit は失敗しても黙って諦めるので、finally の中で呼んでよい。
        for event, fields in deferred_audit:
            board.audit(event, **fields)
    return result


def _say(text: str, *, err: bool = False) -> None:
    """**掲示板へ書いた後の出力は、絶対に例外を出さない。**

    宣言や相乗りを掲示板へ書いたあとに ``print`` が落ちると、**申告だけが残って
    ジョブは 1 度も走らない**。相乗りの幽霊判定は ``since < 起動時刻`` しか無いので、
    残った申告は再起動か ``--force`` まで消えない。ラッパーを作った動機の裏返しである。

    落ちる経路は現実にある。出す内容は**他セッションが書いた自由記述**で、
    Windows のコンソールは cp932 なので ``UnicodeEncodeError`` になりうる。
    ``head`` へパイプすれば ``BrokenPipeError``、ディスクが一杯なら ``OSError``。
    """
    try:
        print(text, file=sys.stderr if err else sys.stdout)
    except Exception:  # noqa: BLE001 - 出せなかったことが作業の成否に影響してはならない
        pass


def _print_occupants(board: Board, resource_id: str) -> None:
    """**今この資源に入っている全員**を出す。相乗りを申告した直後に必ず呼ぶ。

    相乗りには取得の排他が無い。**それは欠陥ではなく目的である**（何人でも入れるための
    仕組みである）。しかしその帰結として、各自が「空きを確かめてから入る」を守っても、
    **同時に入れば合計は超過する**。実測: 「残り 5GB まで可」と申告された資源へ、
    3GB のつもりの 4 者が同時に入り、合計 12GB になった。全員が正しく振る舞っている。

    本ツールはこれを防げない。``--peak`` は単位も尺度も資源ごとに違い（VRAM の GB、
    CPU のコア数、API のリクエスト毎分）、足し算をするにはその資源を知る必要がある
    （CLAUDE.md「Resource Agnosticism」）。**防げない代わりに、隠さない。**

    読み直すのが「入れたあと」なのが要点である。入る前に読んでも、ほぼ同時に入った相手は
    まだ載っていない。**申告し終えてから読めば、相手は必ず見える。** 見えれば降りられる。
    """
    primary = board.read(resource_id)
    joins = board.list_joins(resource_id)
    if primary is None and not joins:
        return

    _say("  現在この資源に入っている（自分を含む）:")
    if primary is not None:
        usage = primary.usage or {}
        estimate = f"  見積 peak={usage.get('peak') or '-'} avg={usage.get('avg') or '-'}"
        _say(f"    主宣言 {primary.session} / {primary.job}{estimate}")
    for join in joins:
        usage = join.usage or {}
        estimate = f"  見積 peak={usage.get('peak') or '-'} avg={usage.get('avg') or '-'}"
        _say(f"    相乗り {join.session} / {join.job}{estimate}")
    _say("  本ツールは見積もりを足し算しない（単位も尺度も資源ごとに違う）。")
    _say("  合計が収まるかは自分で確かめること。収まらないなら rb release --join で降りる。")


def acquire_join(
    board: Board, resource_id: str, args: argparse.Namespace, *, log: str
) -> Acquisition:
    """相乗りとして申告する。``rb run --join`` の取得側。

    ``acquire`` と違い**取得の競合が無い**（相乗りは何人でも入れる）。したがって
    ``EXIT_BUSY`` を返す経路も持たない。判断するのは「主宣言があるか」だけである。

    ラッパーから相乗りできるようにしたのは、**相乗りの取り下げ忘れが主宣言より
    危険だからである。** 相乗りの幽霊判定は ``since < 起動時刻`` しか無く、猶予も
    PID も使わない。取り下げ忘れた相乗りは**再起動か ``--force`` まで残り続け**、
    その資源を「使用中」に固定する。手で ``rb join`` してから手で ``rb release`` する
    運用では、この取りこぼしが必ず起きる。

    Returns
    -------
    Acquisition
        ``declared`` は**このプロセスが申告を作ったか**を表す。既存の申告を
        再利用した場合と掲示板に残せなかった場合は False にし、後始末で
        **自分が作っていない申告を消さない**ようにする。
    """
    primary = board.read(resource_id)
    if primary is None:
        print(
            "その資源に主宣言がありません。--join を外して主宣言として実行してください",
            file=sys.stderr,
        )
        return Acquisition(None, EXIT_USAGE)

    # claim と違い `--found busy` でも止めない。相乗りは使われていることが前提である。
    #
    # **PID を記録する。** ラッパーはジョブと同じ寿命を持つので、生存確認が意味を持つ
    # （手動の `rb join` は記録しない。あちらを実行するのは CLI プロセスであり、
    # すぐ終わるので生存確認が「死んでいる」を返すだけになる）。
    #
    # **記録するだけで、判定は増やさない。** 退去の根拠は 3 つだけである
    # （DESIGN.md「Liveness Judgment」）。`pid_alive` は「単独の根拠にしてはならない」と
    # 自分で宣言している材料なので、これを 4 つ目の根拠に格上げしない。
    # ラッパーが強制終了された相乗りは `rb status` に PID が出るので、
    # 読んだ側が `rb release --join` か `--force` を選べる。
    entry = build_entry(
        resource_id,
        job=args.job,
        display=args.display or "",
        log=log,
        pid=os.getpid(),
        observed={"note": args.observed, "found": args.found},
        eta=args.eta,
        peak=args.peak or "",
        avg=args.avg or "",
        sharing=args.sharing or "",
    )
    result = board.add_join_detailed(entry, os.getcwd(), unique=True)

    if result is JoinResult.EXISTS:
        # 既にある申告はこのプロセスのものとは限らない。**実行は通すが取り下げない。**
        # 消すと、同じ場所で走っている別のジョブの申告を消すことになる。
        print(
            "既に相乗りを申告済みです。その申告のまま実行します（終了時に取り下げません）",
            file=sys.stderr,
        )
        return Acquisition(entry, EXIT_OK, declared=False)
    if result is JoinResult.FAILED:
        # 掲示板に書けないのは**インフラの故障**であって資源の競合ではない。
        # 実行は通す（fail-open）が、残せていないことは隠さない。
        print(
            "警告: 相乗りを掲示板に残せていません。他セッションからは見えません",
            file=sys.stderr,
        )
        return Acquisition(entry, EXIT_OK, declared=False)

    _say(f"相乗りを申告しました: {naming.label(resource_id, entry.display)} / {entry.job}")
    _say(f"  主宣言: {primary.session} / {primary.job}")
    if primary.sharing:
        _say(f"  保持者の相乗り方針: {primary.sharing}")
    else:
        _say("  保持者は相乗り可否を書いていません。合意が取れているか確認すること")
    # **入れたあとに読み直す。** ほぼ同時に入った相手はここで初めて見える。
    _print_occupants(board, resource_id)
    return Acquisition(entry, EXIT_OK, declared=True)


def _explain_failed_displacement(removal: RemovalResult) -> str:
    """幽霊を退けられなかった理由を 1 行で説明する。

    **「消せなかった」を「他人が取った」と混ぜない。** 対処が違う（前者は掲示板の掃除、
    後者は待機）。どちらの場合も取得は諦める。
    """
    if removal is RemovalResult.FAILED:
        return "退けようとした宣言を消せませんでした（掲示板に残っています。監査ログを参照）"
    return "退けようとした宣言が入れ替わりました（他セッションが先に取り直した可能性）"


def _cmd_claim(args: argparse.Namespace) -> int:
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)

    # 手動の claim では PID を記録しない（CLI プロセスは即座に終了するため）。
    result = acquire(
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
    if result.entry is None:
        return result.code

    if not result.declared:
        _warn_not_declared()
        return result.code

    print(f"宣言しました: {result.entry.display} / {result.entry.job}")
    return EXIT_OK


def _warn_not_declared() -> None:
    """掲示板に残せていないことを強く伝える。**実行や作業は止めない**（fail-open）。

    ``acquire`` は掲示板が書けない・壊れたファイルが居座る場合でも ``entry`` を返し、
    ``declared=False`` で通す。ここを分岐せずに「宣言しました」と出すと、
    **他セッションから見えない利用を成功した宣言として偽装する**ことになる。
    掲示板の唯一の役目は「他セッションから見えること」なので、そこが果たせて
    いないなら成功と言ってはならない。
    """
    # **_say で出す。** ここは掲示板へ書いた後であり、出力が例外を出すと
    # 「宣言だけ残ってジョブが 1 度も走らない」になる（`_say` の docstring 参照）。
    _say("警告: 宣言を掲示板に残せていません。他セッションからは見えません", err=True)
    _say("  他セッションはこの利用を知らないまま同じ資源を取りにきます", err=True)
    _say("  作業は止めませんが、衝突を避けたいなら掲示板の状態を確かめること", err=True)


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

    if args.join and args.force:
        # --force は「他者の主宣言を退けて取る」であり、--join は「退けずに入る」である。
        # 併用を黙ってどちらかに倒すと、相乗りのつもりで他人の宣言を消しうる。
        print("--join と --force は同時に指定できません", file=sys.stderr)
        return EXIT_USAGE

    board = Board(args.home)
    resource_id = naming.normalize(args.res)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)
    log_path = Path(args.log) if args.log else runner.build_log_path(board.root, resource_id)
    # 掃除は**本ツールのログ置き場だけ**に対して行う。書き込み先（--log で指定されうる）を
    # 掃除すると、利用者のプロジェクト配下にある無関係な *.log を消す。
    runner.prune_own_logs(board.root)

    if args.join:
        # 相乗りとして入る。取得の競合が無いので `--force` は意味を持たない。
        result = acquire_join(board, resource_id, args, log=str(log_path))
    else:
        # ラッパーはジョブと同じ寿命を持つ。ここでだけ PID を記録する。
        result = acquire(
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
    entry = result.entry
    if entry is None:
        print("資源を取得できなかったため、コマンドを実行していません", file=sys.stderr)
        return result.code

    # **宣言を作ったら、次の行から try に入る。** 間に置いた print が
    # `UnicodeEncodeError`（Windows のコンソールは cp932）や閉じたパイプの
    # `OSError` で落ちると、宣言だけが掲示板に残る。
    # 子の終了コード。**後始末で監査ログに残すために保持する。** 解放の記録が
    # 「rb run の終了」だけだと、走らずに即死したジョブと完走したジョブが同じ 1 行になり、
    # 後から振り返ったときに区別できない（CLAUDE.md「Silence Is Not Success」）。
    # ラッパー自身が落ちて finally だけが通った場合は None のままにする。
    exit_code: int | None = None
    try:
        # **記録できていないのに「宣言しました」と言わない。** 掲示板が書けなかった場合も
        # 実行は通す（fail-open）が、他セッションから見えない利用を成功した宣言として
        # 偽装してはならない。
        if args.join:
            # 相乗りの説明は acquire_join が結果に応じて既に出している。
            # ここで `_warn_not_declared` を足すと、「既に申告済み」の場合に
            # **掲示板に載っているのに載っていないと言う**ことになる。
            pass
        elif result.declared:
            _say(f"宣言しました: {entry.display} / {entry.job}")
        else:
            _warn_not_declared()
        _say(f"ログ: {log_path}")
        exit_code = runner.execute(list(args.trailing), log_path, spawn=SPAWN)
        return exit_code
    except (KeyboardInterrupt, runner.Terminated):
        # **SIGTERM / SIGHUP もここへ落とす。** 既定のハンドラのままだと `finally` を
        # 通らずに死に、宣言だけが残る。残った宣言は PID が死んでいるため、
        # **幽霊判定に最も拾われやすい形**で残る（孤児のジョブはまだ走っている）。
        _say("中断されました", err=True)
        exit_code = EXIT_INTERRUPTED
        return EXIT_INTERRUPTED
    finally:
        _release_after_run(
            board,
            resource_id,
            entry,
            declared=result.declared,
            exit_code=exit_code,
            joined=args.join,
        )


def _release_after_run(
    board: Board,
    resource_id: str,
    entry: Entry,
    *,
    declared: bool,
    exit_code: int | None = None,
    joined: bool = False,
) -> None:
    """``rb run`` の後始末。**ここから例外を出さない。**

    ``finally`` の中で例外が出ると、子プロセスが 0 で終わっていても呼び出し側には
    ラッパーの故障（126）が返る。print 1 つで終了コードが変わってはならない。

    Parameters
    ----------
    exit_code : int, optional
        子プロセスの終了コード。**解放の理由に添えて監査ログへ残す。**
        ラッパー自身が落ちた場合は None（「分からない」であって「成功」ではない）。
    joined : bool
        相乗りとして入ったか。**主宣言と取り違えない。** 相乗りで走ったのに
        主宣言を消しにいくと、他セッションの生きた宣言を消すことになる。
    """
    try:
        if not declared:
            return  # そもそもこのプロセスは申告を作っていない。消すものが無い

        # 終了コードを理由に含める。**成否を解釈はしない**（0 が成功とは限らない資源もある）。
        # 値をそのまま運び、意味づけは振り返る側に任せる。
        code = "不明" if exit_code is None else str(exit_code)
        reason = f"rb run の終了（exit={code}）"

        if joined:
            # **相乗りの取り下げ忘れは主宣言より危険である。** 幽霊判定が
            # `since < 起動時刻` しか無いため、残ると再起動か --force まで消えない。
            # **自分が作った申告だけを消す。** nonce を渡さないと祖先フォールバックが
            # 他セッションの生きた申告を選びうる（cwd だけでは区別できない）。
            removed_join = board.remove_own_join(
                resource_id, os.getcwd(), reason=reason, expect_nonce=entry.nonce
            )
            if removed_join is not None:
                print(f"相乗りを取り下げました: {naming.display_default(resource_id)}")
            else:
                # **「消さなかった」を「消せなかった」と混ぜない。** 走行中に外部から
                # 消えている場合（--force、再起動掃除）に「掲示板に残っています」と
                # 出すと嘘になり、しかもその助言（rb release --join）へ誘導してしまう。
                _say(
                    "相乗りを取り下げませんでした（既に掲示板に無いか、自分の申告ではありません）",
                    err=True,
                )
            return

        # **自分の宣言であることを確かめてから消す。** 実行中に他セッションが --force で
        # 取り直していた場合、無条件に消すと生きた宣言を消してしまう。掲示板は空・資源は
        # 掴まれたままという、最も検出しにくい不整合になる。
        result = board.remove_if_owned(resource_id, reason=reason, nonce=entry.nonce)
        if result is RemovalResult.REMOVED:
            print(f"解放しました: {entry.display}")
        elif result is RemovalResult.ABSENT:
            print(f"宣言は既に掲示板にありません: {entry.display}", file=sys.stderr)
        elif result is RemovalResult.NOT_OWNED:
            print(
                "解放をやめました: 宣言が自分のものではなくなっています"
                "（実行中に他セッションが取り直した可能性）",
                file=sys.stderr,
            )
        else:
            print(
                f"解放に失敗しました: {entry.display}"
                "（掲示板に宣言が残っています。rb release で外すこと）",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 - 後始末の失敗で終了コードを変えない
        pass


def _held_seconds(entry: Entry) -> float | None:
    """宣言してからの経過秒数。時刻が読めなければ None。

    **これは表示のための値であって、判断には使わない。** 長く持っていることは
    幽霊である証拠にならない（CLAUDE.md「Liveness Judgment」）。9 時間の宣言が
    正当なことは実際にある。古さが一目で分かるようにするだけである。
    """
    since = entry.since_dt
    if since is None:
        return None
    return (clock.now() - since).total_seconds()


def _held_for(entry: Entry) -> str:
    """宣言してからの経過を人が読める長さで返す。読めなければ空文字。"""
    seconds = _held_seconds(entry)
    return "" if seconds is None else _format_duration(seconds)


#: 待っている側に必ず渡す助言。**本ツールは実測が空きでも宣言を勝手に退けない**ため、
#: 「掲示板が古いまま」の状態から抜ける道は人間か保持者しかない。それを黙っていると、
#: 待っている側は待ち続けるしかなくなる（実際に 2 時間 48 分待たせた）。
WAIT_ADVICE = (
    "  待っている間に自分でも資源の状態を調べること。"
    "空いているのに宣言が残っているなら、\n"
    "  保持者に確認するか、確認が取れなければ人間に相談すること"
    "（本ツールは実測が空きでも宣言を退けない）"
)


def _cmd_wait(args: argparse.Namespace) -> int:
    """資源が解放されるまで待つ。

    ETA では打ち切らない。掲示板の ETA は申告であって約束ではないため、
    過ぎたからといって待機をやめる根拠にはしない（CLAUDE.md「Time Handling」）。
    打ち切るのは呼び出し側が指定した ``--timeout`` だけである。
    """
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)

    # **入口の基準を本体（wait_for_room）とそろえる。** 主宣言だけを見ると、
    # 相乗りだけが残った資源で「既に解放されています」と答えてしまう。
    # 実際に使っている者がいるのに解放と報告するのが最も危ない誤りである。
    if not waiting.holder_keys(board, resource_id):
        print(f"既に解放されています: {naming.display_default(resource_id)}")
        return EXIT_OK

    entry = board.read(resource_id)
    if entry is None:
        joins = board.list_joins(resource_id)
        print(
            f"待機します: {naming.display_default(resource_id)}"
            f" <- 相乗り {len(joins)} 件（主宣言は無し）"
        )
    else:
        print(
            f"待機します: {naming.label(resource_id, entry.display)}"
            f" <- {entry.session} / {entry.job}"
        )
        held = _held_for(entry)
        print(f"  since {entry.since}{f'（{held} 経過）' if held else ''}")
        if entry.eta:
            stated = entry.eta.get("stated") if isinstance(entry.eta, dict) else None
            at = entry.eta.get("at") if isinstance(entry.eta, dict) else None
            print(f"  ETA   {stated}{f'（{at} 頃）' if at else ''}  ※申告であって約束ではない")
        if entry.log:
            print(f"  log   {entry.log}")
    print(f"  {args.interval:g} 秒ごとに確認、上限 {args.timeout:g} 秒。Ctrl+C で中断できます")
    print(WAIT_ADVICE)

    try:
        result = waiting.wait_for_room(
            board, resource_id, interval_s=args.interval, timeout_s=args.timeout
        )
    except KeyboardInterrupt:
        print("中断しました（宣言はそのままです）", file=sys.stderr)
        return EXIT_INTERRUPTED

    if result.reason == waiting.RELEASED:
        print(f"全ての宣言が消えました（{result.polls} 回確認 / {result.waited_s:.0f} 秒）")
        print("使う前にもう一度自分で状態を調べること（解放＝空きとは限らない）")
        return EXIT_OK

    if result.reason == waiting.SHRANK:
        print(
            f"宣言が減りました（残り {result.holders} 件"
            f" / {result.polls} 回確認 / {result.waited_s:.0f} 秒）"
        )
        print("入れるかどうかは自分で調べて判断すること。駄目ならもう一度 rb wait すればよい")
        return EXIT_OK

    # **上限で戻るときこそ助言が要る。** ここで黙ると、待っている側は同じ待機を
    # 繰り返すしかない。掲示板が古いまま固まっている場合、そこから抜ける道は
    # 保持者か人間しかなく、本ツールは自力で退けられない。
    print(
        f"上限に達しました（{result.polls} 回確認 / {result.waited_s:.0f} 秒）。まだ使用中です",
        file=sys.stderr,
    )
    holder = board.read(resource_id)
    if holder is not None:
        held = _held_for(holder)
        print(
            f"  保持者: {holder.session} / {holder.job}{f'（{held} 前から）' if held else ''}",
            file=sys.stderr,
        )
    print(WAIT_ADVICE, file=sys.stderr)
    return EXIT_BUSY


def _parse_at(value: object) -> datetime | None:
    """監査ログの ``at`` を読む。壊れていれば None。

    監査ログは追記が原子的でないため（Windows）、行が壊れうる前提で書く。
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_duration(seconds: float) -> str:
    """秒を人が読める長さにする。``1h51m`` / ``13m`` / ``42s``。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours, rest = divmod(round(minutes), 60)
    return f"{hours}h{rest:02d}m"


def _pair_key(record: dict) -> tuple[str, str, str]:
    """対応付けの鍵。**主宣言は資源ごと、相乗りは資源と作業ディレクトリごと。**

    相乗りは同じ資源に何人でも入れるので、資源だけで対応させると
    **別セッションの取り下げを自分の申告に結び付ける**。実所要が実際と無関係な値になる。
    """
    event = str(record.get("event", ""))
    resource = str(record.get("resource", ""))
    if event in ("joined", "join_removed"):
        return ("join", resource, str(record.get("cwd", "")))
    return ("primary", resource, "")


def _match_releases(claims: list[dict], removals: list[dict]) -> list[dict | None]:
    """時刻順に並んだ宣言へ、その直後の解放を 1 件ずつ対応させる。

    掲示板は資源ごとに主宣言 1 件なので、ある宣言を閉じるのは「同じ資源で、
    その宣言より後に来た最初の解放」である。対応が付かないものは None になる
    （まだ実行中か、解放がログの窓の外にある）。**None を「まだ実行中」と
    断定しない**。監査ログを何日分読んだかで変わるためである。

    Parameters
    ----------
    claims : list of dict
        ``at`` の昇順に並んだ ``claimed`` レコード。
    removals : list of dict
        同じ窓から拾った ``removed`` レコード。順不同でよい。

    Returns
    -------
    list of (dict or None)
        ``claims`` と同じ並び・同じ長さ。
    """
    Key = tuple[str, str, str]
    pending: dict[Key, list[tuple[datetime, dict]]] = {}
    for record in removals:
        when = _parse_at(record.get("at"))
        if when is None:
            continue
        pending.setdefault(_pair_key(record), []).append((when, record))
    for items in pending.values():
        items.sort(key=lambda item: item[0])

    # 鍵ごとに「どこまで使ったか」を持ち、同じ解放を 2 つの宣言に割り当てない。
    used: dict[Key, int] = {}
    matched: list[dict | None] = []
    for claim in claims:
        started = _parse_at(claim.get("at"))
        key = _pair_key(claim)
        # 相乗りの一斉取り下げ（``rb release --force``）は cwd を持たない。自分の場所の
        # 取り下げが見つからなければ、そちらの箱も見る。
        candidates: list[Key] = [key] if key[0] == "primary" else [key, ("join", key[1], "")]
        chosen: dict | None = None
        for candidate in candidates:
            items = pending.get(candidate, [])
            index = used.get(candidate, 0)
            while index < len(items) and started is not None and items[index][0] < started:
                index += 1
            used[candidate] = index
            if index < len(items):
                chosen = items[index][1]
                used[candidate] = index + 1
                break
        matched.append(chosen)
    return matched


def _elapsed_and_stated(record: dict, release: dict | None) -> tuple[float | None, float | None]:
    """実所要と申告の長さを秒で返す。**どちらも機械が書いた時刻の差である。**

    申告の長さは、自由記述の ``40m`` をここで読み直すのではなく、``claim`` の時点で
    機械が計算した絶対時刻（``eta.at``）との差を使う。同じ文字列を 2 か所で
    解釈すると、解釈がずれたときにどちらが正しいか分からなくなる。

    ここで出す値は**人間が申告の精度を振り返るための表示**であり、
    ツールの判断には一切使わない（CLAUDE.md「Time Handling」）。
    """
    started = _parse_at(record.get("at"))

    elapsed: float | None = None
    if started is not None and release is not None:
        ended = _parse_at(release.get("at"))
        if ended is not None:
            elapsed = (ended - started).total_seconds()

    stated: float | None = None
    eta = record.get("eta")
    if started is not None and isinstance(eta, dict):
        due = _parse_at(eta.get("at"))
        if due is not None:
            stated = (due - started).total_seconds()

    return elapsed, stated


def _cmd_history(args: argparse.Namespace) -> int:
    """過去の宣言を振り返る。見積もりの精度を上げるための材料である。"""
    board = Board(args.home)
    target = naming.normalize(args.resource) if args.resource else None

    # limit ≤ 0 を素通しすると `records[-0:]` が全件になる。「0 件出す」つもりの指定で
    # 数万行が出るのは事故なので弾く。
    limit = max(1, args.limit)

    records = []
    # 解放も一緒に拾う。**実所要は宣言と解放の時刻差から出す**（どちらも機械生成）。
    # 同じ資源の解放は必ず宣言より後に来るので、宣言を拾えた窓には解放も入っている。
    removals: list[dict] = []
    try:
        paths = sorted(board.audit_dir.glob("*.jsonl"))
    except OSError:
        paths = []
    # 新しい日付から遡り、必要な件数が集まったら読むのをやめる。監査ログは
    # `wait_poll` が 10 秒ごとに 1 行足すため放っておくと膨らむ。全件読むと
    # 数か月分を毎回舐めることになる。
    for path in reversed(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if target and record.get("resource") != target:
                continue
            # **相乗りも振り返りの対象にする。** 落とすと、相乗りで走ったジョブだけ
            # 申告と実績を突き合わせられない。見積もりを上げたいのは同じである。
            if record.get("event") in ("claimed", "joined"):
                records.append(record)
            elif record.get("event") in ("removed", "join_removed"):
                removals.append(record)
        # 新しい日から順に見ているので、必要数が集まったらそれ以上は遡らない
        if len(records) >= limit:
            break

    records = sorted(records, key=lambda r: str(r.get("at", "")))[-limit:]
    releases = _match_releases(records, removals)

    if args.json:
        claims = []
        for record, release in zip(records, releases, strict=True):
            elapsed, stated = _elapsed_and_stated(record, release)
            claims.append(
                {
                    **record,
                    "released_at": release.get("at") if release else None,
                    "release_reason": release.get("reason") if release else None,
                    "elapsed_seconds": elapsed,
                    "stated_seconds": stated,
                }
            )
        print(json.dumps({"claims": claims}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not records:
        print("過去の宣言は見つかりませんでした")
        return EXIT_OK

    # **全体を均した値は出さない。** 案件ごとに規模も予測しやすさも違うので、
    # それらをまたいだ中央値や平均は意味を持たない。しかも集計値は
    # 「次は 1/4 で申告すればいい」という機械的な補正を誘い、ETA を必須にした目的
    # （一度考えさせる）と正反対に働く。突き合わせるのは同じ案件の前回である。
    for record, release in zip(records, releases, strict=True):
        eta = record.get("eta") or {}
        usage = record.get("usage") or {}
        eta_text = eta.get("stated") if isinstance(eta, dict) else None
        peak = usage.get("peak") if isinstance(usage, dict) else None
        avg = usage.get("avg") if isinstance(usage, dict) else None
        elapsed, stated = _elapsed_and_stated(record, release)

        # 主宣言と相乗りは別物なので、見分けが付く形で出す。
        kind = "相乗り " if record.get("event") == "joined" else ""
        print(
            f"{record.get('at', '?')}  "
            f"{kind}{naming.display_default(str(record.get('resource', '?')))}"
        )
        print(f"    job   {record.get('job', '')}")

        actual = "解放の記録なし" if elapsed is None else _format_duration(elapsed)
        if eta_text and elapsed is not None and stated:
            # 比を出すのは**同じ案件の中**だけ。ここは自分の申告と自分の実績の対比である。
            print(f"    ETA   {eta_text}  →  実績 {actual}（{elapsed / stated:.2f} 倍）")
        elif eta_text:
            print(f"    ETA   {eta_text}  →  実績 {actual}")
        else:
            print(f"    実績  {actual}")

        if peak or avg:
            print(f"    見積  peak={peak or '-'} avg={avg or '-'}")
        if release is not None and release.get("reason"):
            print(f"    終了  {release['reason']}")

    print()
    print("同じ案件の前回の申告と実績を突き合わせ、次の申告の精度を上げること")
    return EXIT_OK


def _cmd_join(args: argparse.Namespace) -> int:
    """相乗りを申告する。

    **相乗りしてよいかは本ツールが決めない。** 保持者が ``--sharing`` に何を書いていようと、
    可否は当事者の合意事項である。ここでやるのは「入ったことを見えるようにする」ことだけで、
    黙って入られるより遥かによい。

    保持者の宣言（相乗り可否・見積もり・ログ）を読んだうえで判断すること。
    """
    board = Board(args.home)
    resource_id = naming.normalize(args.res)
    primary = board.read(resource_id)

    if primary is None:
        print("その資源に宣言がありません。相乗りではなく rb claim / rb run を使ってください")
        return EXIT_USAGE

    # claim と違い、`--found busy` でも止めない。相乗りは「使われている」ことが前提だからである。

    cwd = os.getcwd()
    entry = build_entry(
        resource_id,
        job=args.job,
        display=args.display or "",
        log=args.log,
        observed={"note": args.observed, "found": args.found},
        eta=args.eta,
        peak=args.peak or "",
        avg=args.avg or "",
        sharing=args.sharing or "",
    )
    # **「既にある」と「残せなかった」を畳まない。** 畳むと、mkdir・書き込み・ハードリンクの
    # いずれが失敗しても「既に申告しています」と答えることになり、掲示板に 1 件も残って
    # いないのに利用者を安心させる。他セッションから見えない利用が始まる。
    result = board.add_join_detailed(entry, cwd)
    if result is JoinResult.EXISTS:
        print("既に相乗りを申告しています（同じ作業ディレクトリから二重には申告できません）")
        return EXIT_OK
    if result is JoinResult.FAILED:
        # 掲示板に書けないのは**インフラの故障**であって資源の競合ではない。
        # 作業は止めない（fail-open）が、成功とは言わない。
        print(
            "警告: 相乗りを掲示板に残せていません。他セッションからは見えません",
            file=sys.stderr,
        )
        print("  掲示板が作れない・書けない可能性がある（監査ログを参照）", file=sys.stderr)
        return EXIT_OK

    # **掲示板へ書いた後なので `_say` を通す。** ここは `rb run --join` と違って
    # try/finally が無く、print が落ちると申告だけが残って何も撤収されない。
    # しかも `claim` の拒否メッセージがこの経路へ利用者を誘導している。
    _say(f"相乗りを申告しました: {entry.display} / {entry.job}")
    _say(f"  主宣言: {primary.session} / {primary.job}")
    if primary.sharing:
        _say(f"  保持者の相乗り方針: {primary.sharing}")
    else:
        _say("  保持者は相乗り可否を書いていません。合意が取れているか確認すること")
    # **入れたあとに読み直す。** ほぼ同時に入った相手はここで初めて見える。
    _print_occupants(board, resource_id)
    return EXIT_OK


def _cmd_update(args: argparse.Namespace) -> int:
    """自分の宣言の申告値を書き換える。

    ジョブが進めば見積もりも ETA も変わる。掲示板を実態へ寄せられないと、
    古い申告が残って他セッションの判断を誤らせる。

    **他者の宣言は書き換えない**（``--force`` を除く）。掲示板は各自の申告の集まりであり、
    人の申告を勝手に直せると、誰の言葉なのか分からなくなる。
    """
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)

    # **読んで、所有を確かめて、書く**までを排他区間にする。この間に他セッションが
    # `claim --force` で取り直すと、他人の宣言を自分の申告で上書きしてしまう。
    with board.locked(resource_id) as lock:
        _warn_if_unlocked(board, resource_id, lock, event="update_unlocked")
        return _update_locked(board, resource_id, args)


def _update_locked(board: Board, resource_id: str, args: argparse.Namespace) -> int:
    """``update`` の本体。呼び出し側がロックを保持している前提である。"""
    entry = board.read(resource_id)

    if entry is None:
        print("宣言が見つかりませんでした", file=sys.stderr)
        return EXIT_USAGE

    if not args.force and not board.owns(
        entry, cwd=os.getcwd(), session_id=platform_info.session_id()
    ):
        print(
            "他セッションの宣言は書き換えられません（--force で上書きできます）", file=sys.stderr
        )
        print(f"  宣言者: {entry.session} / {entry.job}", file=sys.stderr)
        return EXIT_BUSY

    # 読んでから書くまでの間に保持者が入れ替わっていたら、古い内容で潰さない。
    # since は秒精度なので照合に使わない（同じ秒の解放と再取得を見分けられない）
    expect_nonce = entry.nonce

    if args.job is not None:
        entry.holder["job"] = args.job
    if args.log is not None:
        entry.log = args.log
    if args.eta is not None:
        entry.eta = build_eta(args.eta)
    if args.peak is not None or args.avg is not None:
        usage = dict(entry.usage or {})
        if args.peak is not None:
            usage["peak"] = args.peak
        if args.avg is not None:
            usage["avg"] = args.avg
        entry.usage = usage
    if args.sharing is not None:
        entry.sharing = args.sharing

    # **競合と I/O 失敗を別の文言で伝える。** 畳むと、共有違反で書けなかっただけなのに
    # 「宣言が変わった」という事実と違う説明になり、読んだ側が誤った対処をする。
    result = board.replace(entry, reason="update コマンド", expect_nonce=expect_nonce or None)
    if result is UpdateResult.CONFLICT:
        print(
            "更新をやめました: 読んでから書くまでに宣言が入れ替わりました"
            "（他セッションが取り直した可能性）",
            file=sys.stderr,
        )
        return EXIT_BUSY
    if result is UpdateResult.FAILED:
        # 掲示板に書けないのは**インフラの故障**であり、資源の競合ではない。
        # ここを 1 に倒すと、掲示板が壊れた瞬間に呼び出し側が「使用中」と読む。
        print("更新できませんでした（掲示板に書けません。監査ログを参照）", file=sys.stderr)
        return EXIT_OK

    print(f"更新しました: {entry.display} / {entry.job}")
    return EXIT_OK


def _warn_if_unlocked(board: Board, resource_id: str, lock: LockState, *, event: str) -> None:
    """ロック無しで続行することを監査ログに残す。

    ロックが取れないことを理由に**やめない**。解放と更新では、宣言を残したまま終わるほうが
    有害（幽霊が資源を占有し続ける）であり、CAS と所有者照合という主防御は失われないためである。
    取得（``acquire``）も同じく続行する。**ロックが取れないことは、資源が使用中である
    証拠を含まない**（:class:`LockState` 参照）。
    """
    if lock is not LockState.ACQUIRED:
        board.audit(event, resource=resource_id, lock=str(lock))


def _cmd_release(args: argparse.Namespace) -> int:
    """自分の宣言を取り下げる。主宣言が自分のものならそれを、無ければ相乗りを外す。"""
    board = Board(args.home)
    resource_id = naming.normalize(args.resource)
    prefer = "primary" if args.primary else ("join" if args.join else None)

    # --force は「主宣言も相乗りもまとめて消す」であり、--primary / --join は「片方を狙う」。
    # 併用を黙って前者に倒すと、相乗りだけ外すつもりで走行中の主宣言まで消える。
    if args.force and prefer is not None:
        print(
            "--force と --primary / --join は同時に指定できません"
            "（--force は主宣言と相乗りをまとめて消します）",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # 読んで、所有を確かめて、消すまでを排他区間にする（`update` と同じ理由）。
    with board.locked(resource_id) as lock:
        _warn_if_unlocked(board, resource_id, lock, event="release_unlocked")
        if args.force:
            return _release_forced(board, resource_id)
        return _release_own(board, resource_id, prefer=prefer)


def _release_forced(board: Board, resource_id: str) -> int:
    """``--force`` の解放。**相乗りも一緒に外す。**

    主宣言だけ消せても、残った相乗りが資源を「使用中」に固定し続ける。
    そうなると ``rb wait`` は二度と戻らず、フックは全セッションに出し続ける。
    強制解放は最後の掃除手段なので、掃除しきれなければ意味がない。

    **読めるかどうかを削除の条件にしない。** 以前は ``read`` が None なら消さずに
    「見つかりませんでした」と答えていたため、JSON が壊れたエントリを消せなかった。
    壊れたファイルは ``status`` に出ず、``wait`` は即座に解放と答え、``claim`` は
    ``O_EXCL`` で永久に失敗する。**その資源について掲示板が恒久的に機能停止し、
    人間がファイルを手で消すしか回復手段が無くなる**。強制解放は最後の手段なので、
    読めないものこそ消せなければならない。
    """
    primary = board.remove_detailed(resource_id, reason="release コマンド（強制）")
    removed_joins = board.remove_joins(resource_id, reason="release コマンド（強制）")
    joins_note = f"相乗り {removed_joins} 件" if removed_joins else ""

    # **「消せなかった」を「無かった」と混ぜない。** 掲示板には残っている。
    if primary is RemovalResult.FAILED:
        print(
            "解放に失敗しました（掲示板に主宣言が残っています。監査ログを参照）", file=sys.stderr
        )
        # 相乗りの掃除結果は主宣言の成否とは別の事実である。落とさずに報告する。
        if joins_note:
            print(f"  ただし{joins_note}は消しました", file=sys.stderr)
        return EXIT_OK

    if primary is RemovalResult.ABSENT and not removed_joins:
        print("宣言は見つかりませんでした（既に解放済みです）")
        return EXIT_OK

    parts = []
    if primary is RemovalResult.REMOVED:
        parts.append("主宣言")
    if joins_note:
        parts.append(joins_note)
    print(f"強制解放しました: {naming.display_default(resource_id)}（{' / '.join(parts)}）")
    return EXIT_OK


def _release_own(board: Board, resource_id: str, *, prefer: str | None = None) -> int:
    """自分の宣言だけを取り下げる。**曖昧なときは黙って選ばず、指定を求める。**

    ここは 5 周にわたって同じ振り子を往復した場所である。順序を変えるたびに、
    直したのと対称な穴が空いた。

    - **相乗りを祖先まで含めて先に見る**と、自分は相乗りしていないのに祖先から出された
      他人の申告が ``owns`` を通り（このマシンは全プロジェクトが 1 つのルートの下にある）、
      それを消して早期 return する。他人の申告だけが消え、自分の主宣言は残る
    - **主宣言を先に見る**と、相乗り者の cwd が主宣言者と同じ（または配下）のとき
      ``owns`` が通り、**他人の主宣言を消す**。ジョブが走っている最中に宣言だけが消える
    - **完全一致の相乗りを最優先**にすると、今度は逆向きが残る。S が主宣言し、T が
      同じ cwd から相乗りしている場面で、**S が ``rb release`` を打つと T の相乗りが
      先に消え、S の主宣言が残る**

    **cwd だけでは S と T を区別できない。** どちらを選んでも片方が壊れるので、
    自動で選ぶのをやめる。両方が候補になったら止めて ``--primary`` / ``--join`` を
    指定させる。片方しか無いときだけ従来どおり自動で選ぶ。

    曖昧とみなすのは**この場所から出された相乗り**（パスの完全一致）が存在する場合だけに
    限る。祖先フォールバックで拾う候補は「自分のものかもしれない」という推測であり、
    そこまで曖昧に数えると、他人がハブのルートから出した相乗りがあるだけで
    自分の主宣言すら解放できなくなる。

    Parameters
    ----------
    prefer : {"primary", "join"}, optional
        どちらを外すか。省略時は候補が 1 つのときだけ自動で選ぶ。
    """
    cwd = os.getcwd()
    entry = board.read(resource_id)
    mine_primary = entry is not None and board.owns(
        entry, cwd=cwd, session_id=platform_info.session_id()
    )
    # 「この場所から出された相乗り」だけを曖昧さの材料にする（上の docstring 参照）。
    # **ファイル名で判定しない。** 鍵にラッパーの nonce が入った時点で恒偽になり、
    # 主宣言と相乗りが両方あるのに「相乗りは無い」と読んで**走行中の主宣言を黙って
    # 解放する**経路になっていた。
    has_exact_join = board.declared_here(resource_id, cwd)

    if prefer is None and mine_primary and has_exact_join:
        return _report_ambiguous_release(resource_id, entry)

    if prefer == "primary":
        if mine_primary and entry is not None:
            return _release_own_primary(board, resource_id, entry)
        return _refuse_primary_release(entry)

    if prefer == "join":
        join = board.remove_own_join(resource_id, cwd, reason="release コマンド（--join）")
        if join is None:
            print("自分の相乗りの申告は見つかりませんでした", file=sys.stderr)
            return EXIT_OK
        _report_join_release(resource_id, join, cwd)
        return EXIT_OK

    # 以降は候補が 1 つしか無い場合である。見る順序は
    # **完全一致の相乗り → 主宣言 → 祖先の相乗り**。
    if has_exact_join:
        exact = board.remove_own_join(resource_id, cwd, reason="release コマンド")
        if exact is not None:
            _report_join_release(resource_id, exact, cwd)
            return EXIT_OK

    if mine_primary and entry is not None:
        return _release_own_primary(board, resource_id, entry)

    join = board.remove_own_join(resource_id, cwd, reason="release コマンド")
    if join is not None:
        _report_join_release(resource_id, join, cwd)
        return EXIT_OK

    return _refuse_primary_release(entry)


def _report_ambiguous_release(resource_id: str, entry: Entry | None) -> int:
    """主宣言と相乗りの両方が候補になったことを伝えて、何も消さずに戻る。

    **黙ってどちらかを選ばない。** 同じ作業ディレクトリから主宣言と相乗りが出ている場合、
    掲示板の情報だけでは「どちらが呼び出し側のものか」を決められない。推測して外すと、
    走っているジョブの宣言を消すか、他人の相乗りを消すかのどちらかになる。
    """
    print("主宣言と相乗りの両方が自分のものの候補です。どちらを外すか指定してください")
    if entry is not None:
        print(f"  主宣言: {entry.session} / {entry.job}（since {entry.since}）")
    print("  相乗り: この作業ディレクトリから出した申告")
    display = naming.display_default(resource_id)
    print(f"  rb release {display} --primary   （主宣言を外す）")
    print(f"  rb release {display} --join      （相乗りを取り下げる）")
    return EXIT_USAGE


def _report_join_release(resource_id: str, join: Entry, cwd: str) -> None:
    """相乗りを外したことを伝える。祖先フォールバックなら申告元も出す。"""
    print(f"相乗りを取り下げました: {naming.display_default(resource_id)}")
    declared = str(join.holder.get("cwd") or "")
    if declared and os.path.normcase(declared) != os.path.normcase(cwd):
        # 完全一致ではなく祖先フォールバックで選んだ。**誤爆が目視できるように**
        # 申告元を出す。出さなければ、他人の申告を消したことに気づく手段が無い。
        print(f"  申告元: {declared}（自分の場所から出した申告ではありません）")


def _refuse_primary_release(entry: Entry | None) -> int:
    """外せる宣言が無かったことを伝える。

    **他人の宣言は消さない。** 掲示板の唯一の強制点は先着排他であり、最も打ちやすい
    コマンドでそれを無効化できてはならない。claim が ``--force`` を要求するのと対称にする。
    """
    if entry is None:
        print("宣言は見つかりませんでした（既に解放済みです）")
        return EXIT_OK

    print("他セッションの宣言は解放できません（--force で強制解放できます）", file=sys.stderr)
    print(f"  宣言者: {entry.session} / {entry.job}", file=sys.stderr)
    print(f"  since : {entry.since}", file=sys.stderr)
    return EXIT_BUSY


def _release_own_primary(board: Board, resource_id: str, entry: Entry) -> int:
    """自分の主宣言を取り下げる。**読んだ宣言と一致するときだけ消す（CAS）。**

    無条件の削除は、掲示板の書き換えの中でここだけが CAS に乗らないという穴になる。
    解放はロックが取れなくても続行する（``_warn_if_unlocked``）ので、次が成立する。
    他セッションが ``claim --force`` で取り直した直後にここへ来ると、``read`` が拾うのは
    **相手の新しい宣言**であり、cwd が同じ（または相手の cwd が自分の祖先）なら
    ``owns`` を通る。**他人の生きた宣言を消して掲示板だけが空になり、資源は掴まれたまま**
    という最も検出しにくい不整合ができる。同一プロジェクトで 2 セッションが動くのは
    日常なので、この条件は容易に成立する。

    :meth:`Board.remove_if_owned` は使わない。呼び出し側（``_cmd_release``）が既に
    ロックを保持しているため、内部の ``locked()`` が**自分のロック**に 2 秒ぶつかって
    無駄な監査イベントを出すだけになる。
    """
    if entry.nonce:
        result = board.remove_if_nonce(
            resource_id, expect_nonce=entry.nonce, reason="release コマンド"
        )
    else:
        # nonce を持たない古いエントリ。照合できる値が無いので従来どおり消す。
        result = board.remove_detailed(resource_id, reason="release コマンド")

    if result is RemovalResult.REMOVED:
        print(f"解放しました: {naming.display_default(resource_id)}")
        return EXIT_OK
    if result is RemovalResult.ABSENT:
        print("宣言は見つかりませんでした（既に解放済みです）")
        return EXIT_OK
    if result is RemovalResult.NOT_OWNED:
        # **「消さなかった」を「消せなかった」と混ぜない。** ここは掲示板が正常に読めた
        # 上で「他者の生きた宣言である」と判定できた場合なので、使用中として 1 を返す。
        print(
            "解放をやめました: 宣言が入れ替わりました（他セッションが取り直した可能性）",
            file=sys.stderr,
        )
        return EXIT_BUSY
    # 削除できなかった。**「無かった」と混ぜない**（掲示板には残っている）。
    print("解放に失敗しました（掲示板に宣言が残っています。監査ログを参照）", file=sys.stderr)
    return EXIT_OK


def _add_declaration_options(parser: argparse.ArgumentParser, *, with_force: bool = True) -> None:
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
    if with_force:
        parser.add_argument(
            "--force", action="store_true", help="他者の宣言を退けて強制的に取得する"
        )


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

    release = sub.add_parser(
        "release",
        help="宣言を解放する（自分のものだけ）",
        description=(
            "自分の主宣言または相乗りを取り下げる。同じ作業ディレクトリから両方が"
            "出ている場合、掲示板の情報だけではどちらが呼び出し側のものか決められないため、"
            "**推測せずに指定を求める**。--primary / --join で狙って外すこと。"
        ),
    )
    release.add_argument("resource", help="資源 ID")
    release.add_argument(
        "--force", action="store_true", help="他セッションの宣言も強制的に解放する"
    )
    # **どちらを外すかの明示。** 両方が候補になるとき、本ツールは選ばずに止める。
    kind = release.add_mutually_exclusive_group()
    kind.add_argument("--primary", action="store_true", help="主宣言のほうを外す")
    kind.add_argument("--join", action="store_true", help="相乗りのほうを取り下げる")
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
    run.add_argument(
        "--join",
        action="store_true",
        help="主宣言を取らず、相乗りとして入る（終了時に相乗りを取り下げる）",
    )
    _add_declaration_options(run)
    run.set_defaults(func=_cmd_run)

    join = sub.add_parser(
        "join",
        help="他者が宣言している資源に相乗りすることを申告する",
        description=(
            "既に宣言のある資源へ相乗りすることを掲示板に載せる。"
            "**相乗りしてよいかは本ツールが決めない**。保持者の相乗り方針を読み、"
            "合意のうえで使うこと。ここでやるのは「入ったことを見えるようにする」ことだけである。"
        ),
    )
    join.add_argument("--res", required=True, help="資源 ID")
    # 相乗りに --force は無い。取得の排他を破る操作ではないため、強制する対象が無い
    _add_declaration_options(join, with_force=False)
    join.set_defaults(func=_cmd_join)

    update = sub.add_parser(
        "update",
        help="自分の宣言を書き換える（見積もりや ETA を実態に合わせる）",
        description=(
            "既に出している宣言の申告値を更新する。ジョブが進んで使用量が変わったときに、"
            "掲示板を実態へ寄せるために使う。"
        ),
    )
    update.add_argument("resource", help="資源 ID")
    update.add_argument("--job", default=None, help="何をするか（1 行）")
    update.add_argument("--eta", default=None, help="終わるまでの見込み")
    update.add_argument("--peak", default=None, help="利用見積もりの瞬時最大")
    update.add_argument("--avg", default=None, help="利用見積もりの平均")
    update.add_argument("--sharing", default=None, help="相乗りの可否と条件")
    update.add_argument("--log", default=None, help="進捗が読めるログのパス")
    update.add_argument("--force", action="store_true", help="他者の宣言でも書き換える")
    update.set_defaults(func=_cmd_update)

    wait = sub.add_parser(
        "wait",
        help="資源を宣言している者が減るまで待つ",
        description=(
            "宣言の数が減る（誰かが解放する）まで待つ。相乗りが**増えた**ときには起きない"
            "（資源はさらに詰まっているため）。ETA では打ち切らない（申告であって約束ではない）。"
            "打ち切るのは --timeout だけである。毎回のポーリングは監査ログに残る。"
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
    except KeyboardInterrupt:
        # **中断を「使用中」に化けさせない。** EXIT_BUSY は 1 なので、traceback で 1 を
        # 返すと呼び出し側から資源の競合と区別できない。シェルの慣習どおり 130 を返す。
        print("中断しました", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - fail-open
        print(f"[resource-broker] 内部エラーのため判定を省略します: {exc}", file=sys.stderr)
        try:
            Board(args.home).audit("cli_internal_error", command=args.command, error=str(exc))
        except Exception:  # noqa: BLE001
            pass
        # **run だけは 0 を返さない。** fail-open は「資源アクセスを止めない」原則であって、
        # 「走らなかったジョブを成功と報告してよい」ではない。ここで 0 を返すと、
        # 引数の不備などで 1 度も起動していないのに呼び出し側が成功と読む。
        command = getattr(args, "command", None)
        if command == "run":
            return runner.EXIT_CANNOT_EXECUTE
        if command == "wait":
            # **wait の 0 は「宣言が減った」という積極的な意味を持つ。** 内部エラーで
            # 0 を返すと、1 度も待っていないのに「空いた」と読まれ、使用中の資源を
            # 掴みにいく。fail-open は「情報が無いなら通す」であって「嘘をつく」ではない。
            #
            # **上限到達（EXIT_BUSY）とも分ける。** どちらも 1 にすると、呼び出し側が
            # 「上限まで待った」と「1 度も待っていない」を区別できない。前者は待ち直す
            # 価値があり、後者は原因を調べる必要がある。対処が違うものを畳まない。
            return EXIT_WAIT_BROKEN
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
