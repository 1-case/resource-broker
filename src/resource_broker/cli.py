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
    LockState,
    RemovalResult,
    UpdateResult,
    build_entry,
    build_eta,
    first_declaration,
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
) -> list[tuple[Verdict, Entry]]:
    """その資源の宣言を**1 件ずつ**判定する。古い順に返す。

    **どれが先に取ったかで扱いを変えない。** 宣言は対等であり、生きているか幽霊かは
    それぞれの ``since`` / ``boot`` / PID で決まる。役割を持たせていた頃は、
    片方（主宣言）が消えるともう片方（相乗り）の意味が変わってしまい、走っている
    作業がいるのに「空き」と答える経路になっていた。

    Parameters
    ----------
    observation : Observation, optional
        セッションが調べた結果。省略時は「調べていない」として扱う。
        **本関数は資源を調べない**。資源の種別で分岐する箇所はここに存在しない。
        実測は資源の状態であって宣言ごとの状態ではないので、**全ての宣言に同じ値を
        渡す**（「使用中」は誰か 1 人でも生きていれば真、という向きで効く）。
    """
    now = clock.now()
    boot = platform_info.boot_time()
    judged: list[tuple[Verdict, Entry]] = []
    for entry in board.list_for(resource_id):
        verdict = liveness.judge(
            has_entry=True,
            since=entry.since_dt,
            boot=boot,
            observation=observation or Observation(),
            pid_alive=platform_info.pid_alive(entry.pid),
            now=now,
        )
        judged.append((verdict, entry))
    return judged


def live_declarations(
    board: Board, resource_id: str, observation: Observation | None = None
) -> list[Entry]:
    """幽霊と判定されなかった宣言だけを古い順に返す。"""
    return [
        e
        for verdict, e in assess(board, resource_id, observation)
        if not liveness.is_free(verdict)
    ]


def _known_resources(board: Board) -> list[str]:
    """掲示板に載っている資源を列挙する。"""
    return _known_resources_detailed(board)[0]


def _known_resources_detailed(board: Board) -> tuple[list[str], bool]:
    """掲示板に載っている資源と、**読めなかったものがあったか**を返す。

    読めなかったことを畳まない。畳むと「掲示板は空です」と断定してしまい、
    **実際には使われている資源を空きとして配る**（DESIGN.md「Liveness Judgment」の
    非対称性の裏返しであり、断定してよい側ではない）。
    """
    entries, unreadable = board.list_all_detailed()
    resources: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.resource not in seen:
            seen.add(entry.resource)
            resources.append(entry.resource)
    return resources, unreadable


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
    """1 つの宣言を機械可読な形にする。**全ての宣言が同じ形である。**"""
    return {
        "holder": entry.holder,
        "display": entry.display,
        "held_for_seconds": _held_seconds(entry),
        "job": entry.job,
        "since": entry.since,
        "log": entry.log,
        "observed": entry.observed,
        "eta": entry.eta,
        "usage": entry.usage,
        "sharing": entry.sharing or None,
    }


def _report_unreadable(board: Board) -> None:
    """読めないファイルの**場所を教える**。

    どの資源のものか分からない（中身が読めない）ので、資源を指して消せない。
    何も塞いではいないが、掲示板が「一部を読めない」と言い続ける原因にはなる。
    黙って放置すると、読む側は理由の分からない留保だけを受け取る。
    """
    paths = board.unreadable_paths()
    if not paths:
        return
    print(f"  読めないファイルが {len(paths)} 件あります（どの資源のものか判別できません）")
    for path in paths[:5]:
        print(f"    {path}")
    if len(paths) > 5:
        print(f"    ほか {len(paths) - 5} 件")
    print("  掃除するなら rb release --clean（資源は指定しない）")


def _cmd_status(args: argparse.Namespace) -> int:
    board = Board(args.home)
    if args.resource:
        targets, unreadable = [naming.normalize(r) for r in args.resource], False
    else:
        targets, unreadable = _known_resources_detailed(board)

    rows = []
    for resource_id in targets:
        judged = assess(board, resource_id)
        living = [e for verdict, e in judged if not liveness.is_free(verdict)]
        # **「空き」と「誰も使っていない」は同じ問いになった。** 役割が無いので、
        # 生きた宣言が 1 件でもあれば使用中、無ければ空きである。分けていた頃は
        # 「主宣言の枠は空いているが相乗りがいる」という状態があり、そこが
        # 「空き」に見えることが事故の元だった。
        occupied = bool(living)
        first = living[0] if living else None
        rows.append(
            {
                "resource": resource_id,
                "display": (first.display if first else "") or naming.display_default(resource_id),
                # 一覧の見出し。**資源 ID を必ず含める**（display による置き換えを許さない）。
                "label": naming.label(resource_id, first.display if first else ""),
                "occupied": occupied,
                "holders": len(living),
                # **幽霊も載せる。** 掲示板にあるものを一覧から消してはならない——
                # 資源が「空き」と出るだけでは、古い宣言が残っていることに気づけず、
                # 誰も掃除しない。判定は 1 件ずつ付ける。
                "declarations": [
                    {
                        **_describe(entry),
                        "verdict": str(verdict),
                        "reason": liveness.explain(verdict),
                    }
                    for verdict, entry in judged
                ],
                "reason": (
                    f"{len(living)} 件の宣言がある"
                    if occupied
                    else liveness.explain(judged[0][0])
                    if judged
                    else "宣言が無い"
                ),
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
            _report_unreadable(board)
            return EXIT_OK
        print("掲示板は空です（誰も資源を宣言していません）")
        print("使う前に自分で資源の状態を調べ、rb claim で宣言すること")
        return EXIT_OK

    if unreadable:
        print("注意: 掲示板の一部を読めませんでした。**これで全部とは限りません**")
        _report_unreadable(board)

    for row in rows:
        mark = "使用中" if row["occupied"] else "空き"
        print(f"{row['label']:<24} {mark:<6} {row['reason']}")

        # **宣言を古い順に並べるだけ。** 役割で分けない——どれが先かは since に出ている。
        for index, declaration in enumerate(row["declarations"], start=1):
            holder = declaration["holder"] or {}
            ghost = " ※幽霊と判定" if declaration.get("verdict") == "free" else ""
            job = declaration["job"] or "(ジョブ未記入)"
            # **PID を出す。** 自動回収は「猶予経過 + 実測空き + PID 死亡」の 3 条件が
            # 揃ったときだけで、揃わない宣言は残る。どれが死んでいるかを読む側が
            # 見分けられないと、記録している意味が無い。
            pid = holder.get("pid")
            marker = f" (pid {pid})" if isinstance(pid, int) and not isinstance(pid, bool) else ""
            # **release --nonce に渡す値をここに出す。** 掲示板を読んだ人が
            # `rb release --nonce <先頭 8 桁>` の 1 手で済むようにするためで、
            # 前方一致で一意に決まらなければ拒否されるので最小桁数の規則は要らない。
            nonce = holder.get("nonce")
            nonce_marker = f" nonce {nonce[:8]}" if isinstance(nonce, str) and nonce else ""
            print(
                f"{'':<24} 宣言{index}  {holder.get('session', '?')}{marker}"
                f"{nonce_marker} / {job}{ghost}"
            )

            held = declaration["held_for_seconds"]
            elapsed = f"（{_format_duration(held)} 経過）" if held is not None else ""
            print(f"{'':<24}        since {declaration['since']}{elapsed}")

            eta = declaration["eta"] or {}
            if eta.get("stated"):
                at = f"（{eta['at']} 頃）" if eta.get("at") else ""
                print(f"{'':<24}        ETA {eta['stated']}{at}  ※申告であって約束ではない")
            usage = declaration["usage"] or {}
            if usage.get("peak") or usage.get("avg"):
                print(
                    f"{'':<24}        見積 瞬時最大 {usage.get('peak') or '-'}"
                    f" / 平均 {usage.get('avg') or '-'}"
                )
            if declaration["sharing"]:
                print(f"{'':<24}        共有 {declaration['sharing']}")
            if declaration["log"]:
                print(f"{'':<24}        log {declaration['log']}")
            observed = declaration["observed"] or {}
            if observed.get("note"):
                print(f"{'':<24}        観測 {observed['note']}")
                print(f"{'':<24}             （{observed.get('at', '時刻不明')} 時点の申告）")

        if row["holders"] > 1:
            print(f"{'':<24} 合計   {row['holders']} 件の宣言")
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
    share: bool = False,
    pid: int | None = None,
) -> Acquisition:
    """宣言を 1 件出す。``claim`` と ``run`` で共通の判断である。

    **生きた宣言があれば断る。** 段差であって壁ではない——``--share`` で並べるし、
    ``--force`` で退けられる。断らなければ掲示板は書き込み専用になり、**見ないから
    作ったツール**という前提が崩れる。

    ``--share`` は「既に使われていることを承知で並ぶ」という**意思表示**であり、
    掲示板には**役割として記録されない**。宣言が 1 件増えるだけである。どれが先に
    取ったかは ``since`` に出ているので、記録する必要が無い（かつての ``rb join``
    は「相乗り」という別の種類の記録を作っていた。それが要らない）。

    Parameters
    ----------
    pid : int, optional
        宣言者として記録する PID。**手動の ``claim`` では渡さない**。
        すぐ終わる CLI プロセスの PID を記録すると、幽霊判定が「死んでいる」を
        返すだけの材料になる。
    """
    notices: list[str] = []
    with board.locked(resource_id) as lock:
        if lock is not LockState.ACQUIRED:
            # **囲えなくても続行する。** ロックは競り合いを減らすだけで、正しさは
            # nonce の CAS が担保している（DESIGN.md「Locking」）。
            #
            # **黙って続行しない。** ロックが取れないのは本ツール側の事情であって
            # 資源の競合ではない。混同すると「使用中」と読まれる。
            notices.append(
                f"[rb] 掲示板のロックを取れませんでした（{lock}）。**排他を弱めて続行**します"
            )
            board.audit("claim_unlocked", resource=resource_id, lock=str(lock))

        judged = assess(board, resource_id, observation)
        ghosts = [e for verdict, e in judged if liveness.is_free(verdict)]
        living = [e for verdict, e in judged if not liveness.is_free(verdict)]

        # **幽霊は退ける。** 根拠は 3 つのいずれかで、判定は 1 件ずつ独立している。
        for ghost in ghosts:
            reason = "強制取得" if force else "幽霊と判定した"
            removal = board.remove_if_nonce(resource_id, expect_nonce=ghost.nonce, reason=reason)
            if removal not in (RemovalResult.REMOVED, RemovalResult.ABSENT):
                # **消せなかったことを「使用中」に化けさせない。** 掲示板のファイルを
                # 消せなかっただけで、資源の保持者を確認したわけではない。平坦化で
                # 幽霊のファイルは何も塞がなくなった（名前が nonce なので新しい宣言は
                # 作れる）ので、退去に失敗しても取得を通してよい。fail-open は
                # 「本ツール側の故障で作業を止めない」ことである。
                notices.append(_explain_failed_displacement(removal))

        # **退去のあとに読み直す。** 幽霊を消している間に他セッションが入っていることが
        # ある（実際、同じ幽霊を見た 2 人のうち一方が先に取り直す）。古い読み取りで
        # 判断すると、**生きた宣言があるのに気づかずに通す**。
        living = live_declarations(board, resource_id, observation)

        if force:
            for entry in list(living):
                if (
                    board.remove_if_nonce(resource_id, expect_nonce=entry.nonce, reason="強制取得")
                    is RemovalResult.REMOVED
                ):
                    living.remove(entry)

        # **生きた宣言があれば断る。** これは「投稿させない」のではなく「止まれ、他に
        # 人がいる。見てから、意図してそうだと言え」である。``--share`` 一つで通るので
        # 壁ではなく段差であり、**この段差が製品そのもの**である——セッションが掲示板を
        # 見ないから作ったツールで、断らなければ掲示板は書き込み専用になる。
        #
        # **役割は記録しない。** ``--share`` を付けて出した宣言は、他の宣言と 1 バイトも
        # 変わらない。どれが先かは ``since`` に出ている。
        # **自分で busy と申告したときも断る。** 掲示板が空でも同じ——誰かが宣言せずに
        # 使っている、という状態であり、実測は単独で確定する（DESIGN.md「実測の非対称性」）。
        if (living or found == "busy") and not share and not force:
            label = naming.label(resource_id, living[0].display if living else "")
            if living:
                notices.append(
                    f"[rb] {label} は使用中です（既に {len(living)} 件の宣言があります）"
                )
            else:
                notices.append(f"[rb] {label} は使用中です（自分で busy と申告している）")
            notices.extend(
                f"  {e.session} / {e.job}（since {e.since}）"
                + (f"  申し送り: {e.sharing}" if e.sharing else "")
                for e in living
            )
            if found == "free":
                # 申告と掲示板が食い違っている。**「空き」は宣言を退ける根拠にならない**
                # （宣言はジョブが資源を掴む前に出る）ので、掲示板の側を採る。
                notices.append("  あなたの申告は free です。**どちらかが古い。**")
            notices.append(
                "  並んで使うなら --share。宣言が古いと判断したなら --force で退けること"
            )
            # **待つ道を必ず示す。** 断るだけで次の手を示さないと、待つ側は
            # 同じコマンドを繰り返すしかない。
            notices.append(f"  空くのを待つなら rb wait {resource_id}")
            for text in notices:
                _say(text, err=True)
            board.audit(
                "claim_refused",
                resource=resource_id,
                reason="生きた宣言がある" if living else "自分で busy と申告している",
                holder=living[0].session if living else None,
                sharing=(living[0].sharing or None) if living else None,
                holders=len(living),
            )
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
        declared = board.declare(new_entry)
        if not declared:
            notices.append("警告: 宣言を掲示板に残せていません（他セッションからは見えません）")

        # 読めないファイルは何も塞がないが、`status` が留保を出し続ける原因になる。
        # 取得の場面でも場所を教えておく（掃除の道を示さないと放置される）。
        stray = board.unreadable_paths()
        if stray:
            notices.append(
                f"[rb] 壊れたエントリが {len(stray)} 件あります（掃除: rb release --clean）"
            )

        # **書いた直後に読み直す。** 平坦にした代償として、`O_EXCL` による「先着 1 名」が
        # 無くなった。読んでから書くまでの窓で 2 セッションが同時に通り抜けられる
        # （実測: 窓を意図的に開くと 12 プロセス全員が通る）。
        #
        # **窓は塞げない。** 条件付き書き込みのプリミティブが OS に無いのは、この
        # プロジェクトが何度も突き当たっている壁である（DESIGN.md「Known Residuals」）。
        # できるのは**見えるようにすること**で、それはこのツールの本来の仕事でもある。
        # ここで読み直せば、同じ瞬間に入った相手は必ず双方から見える——止めないが、
        # 気づかないまま進むことはない。
        if declared:
            latecomers = [
                e
                for e in live_declarations(board, resource_id, observation)
                if e.nonce != new_entry.nonce and e.nonce not in {x.nonce for x in living}
            ]
            if latecomers:
                notices.append(
                    f"[rb] **ほぼ同時に {len(latecomers)} 件の宣言が入りました。**"
                    "先着を決める仕組みはありません"
                )
                notices.extend(f"  {e.session} / {e.job}（since {e.since}）" for e in latecomers)
                notices.append("  重なって困るなら、どちらかが rb release して rb wait すること")
                board.audit(
                    "simultaneous",
                    resource=resource_id,
                    others=len(latecomers),
                )

    if living:
        notices.append(f"[rb] この資源には既に {len(living)} 件の宣言があります")
        notices.extend(
            f"  {e.session} / {e.job}（since {e.since}）"
            + (f" 共有: {e.sharing}" if e.sharing else "")
            for e in living
        )
    for text in notices:
        _say(text, err=True)
    return Acquisition(new_entry, EXIT_OK, declared=declared)


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
        share=args.share,
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

    board = Board(args.home)
    resource_id = naming.normalize(args.res)
    observation = Observation(busy=FOUND_CHOICES.get(args.found), note=args.observed)
    log_path = Path(args.log) if args.log else runner.build_log_path(board.root, resource_id)
    # 掃除は**本ツールのログ置き場だけ**に対して行う。書き込み先（--log で指定されうる）を
    # 掃除すると、利用者のプロジェクト配下にある無関係な *.log を消す。
    runner.prune_own_logs(board.root)

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
        share=args.share,
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
        )


def _release_after_run(
    board: Board,
    resource_id: str,
    entry: Entry,
    *,
    declared: bool,
    exit_code: int | None = None,
) -> None:
    """``rb run`` の後始末。**ここから例外を出さない。**

    ``finally`` の中で例外が出ると、子プロセスが 0 で終わっていても呼び出し側には
    ラッパーの故障（126）が返る。print 1 つで終了コードが変わってはならない。

    **消すのは自分が出した 1 件だけ。** nonce で指すので、走行中に外部から消えて
    いても、他セッションが同じ資源へ宣言していても、取り違えない。

    Parameters
    ----------
    exit_code : int, optional
        子プロセスの終了コード。**解放の理由に添えて監査ログへ残す。**
        ラッパー自身が落ちた場合は None（「分からない」であって「成功」ではない）。
    """
    try:
        if not declared:
            return  # そもそもこのプロセスは宣言を作っていない。消すものが無い

        # 終了コードを理由に含める。**成否を解釈はしない**（0 が成功とは限らない資源もある）。
        code = "不明" if exit_code is None else str(exit_code)
        reason = f"rb run の終了（exit={code}）"

        result = board.remove_own(resource_id, reason=reason, nonce=entry.nonce)
        if result.removed:
            _say(f"解放しました: {naming.label(resource_id, entry.display)}")
        elif result.swapped:
            _say("宣言が入れ替わりました（解放していません）", err=True)
        elif result.failed:
            _say(
                "警告: 宣言を取り下げられませんでした（掲示板に残っています）",
                err=True,
            )
        else:
            # **「消さなかった」を「消せなかった」と混ぜない。** 走行中に外部から
            # 消えている場合（``--force``、再起動掃除）に「掲示板に残っています」と
            # 出すと嘘になる。
            _say(
                "宣言を取り下げませんでした（既に掲示板に無いか、自分の宣言ではありません）",
                err=True,
            )
    except Exception:  # noqa: BLE001 - 後始末の失敗でジョブの結果を変えない
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

    entry = first_declaration(board, resource_id)
    if entry is None:
        print(f"待機します: {naming.display_default(resource_id)}")
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
    holder = first_declaration(board, resource_id)
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
    """対応付けの鍵。資源ごとに、宣言と解放を突き合わせる。

    同じ資源に何件でも宣言が並ぶので、資源だけでは**別セッションの解放を自分の宣言に
    結び付ける**。job も鍵に入れて、同じ資源の別の作業と混ざらないようにする。
    """
    resource = str(record.get("resource", ""))
    return ("declaration", resource, str(record.get("job", "")))


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
        candidates: list[Key] = [key]
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
            if record.get("event") == "claimed":
                records.append(record)
            elif record.get("event") == "removed":
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
        print(
            f"{record.get('at', '?')}  {naming.display_default(str(record.get('resource', '?')))}"
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
    """``update`` の本体。呼び出し側がロックを保持している前提である。

    **「いちばん古い宣言」を掴んではならない。** 平坦化で 1 資源に宣言が N 件並ぶように
    なったので、最古を選ぶと「先に宣言した人のもの」という**役割を暗に復活させる**。
    後から `--share` で並んだ側は自分の宣言を一生更新できず、`--force` を付けると
    **他人の宣言に自分の申告を書き込む**ことになる（掲示板は各自の申告の集まりであり、
    人の申告を勝手に直せると誰の言葉なのか分からなくなる）。
    """
    cwd = os.getcwd()
    session_id = platform_info.session_id()
    declarations = board.list_for(resource_id)
    mine = [e for e in declarations if board.owns(e, cwd=cwd, session_id=session_id)]

    if not declarations:
        print("宣言が見つかりませんでした", file=sys.stderr)
        return EXIT_USAGE

    if mine:
        entry = mine[0]
        if len(mine) > 1:
            # **どれを書き換えたかを言う。** 黙って 1 件選ぶと、更新したつもりの宣言と
            # 実際に変わった宣言が食い違ったまま気づけない。
            _say(
                f"自分の宣言が {len(mine)} 件あります。最も古いものを書き換えます: {entry.job}",
                err=True,
            )
    elif args.force:
        entry = declarations[0]
        # **他人のものを書き換えたことを必ず言う。** 誰の言葉かが変わる操作である。
        _say(f"警告: 他セッションの宣言を書き換えます: {entry.session} / {entry.job}", err=True)
    else:
        print(
            "自分の宣言はありません（--force で他セッションのものを書き換えられます）",
            file=sys.stderr,
        )
        for other in declarations:
            print(f"  {other.session} / {other.job}（since {other.since}）", file=sys.stderr)
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
    """宣言を取り下げる。

    **選ばせるものが無い。** 役割を記録しないので「主宣言か相乗りか」という問いが
    消えた。取り下げるのは**自分の宣言**であり、自分の宣言が 2 件以上あるときは
    どれを消すか決められないので、既定では**何も消さずに** ``EXIT_USAGE`` で拒否する
    （曖昧なまま 1 件選ぶと、黙って間違った方を消しうる。DESIGN.md「採らなかった案」）。
    1 件なら従来どおり消える。
    まとめて消すには ``--all``、資源 ID を省いて 1 本だけ狙って消すには ``--nonce``。
    他人のものまで消すのは ``--force`` だけである——これは ``--nonce`` でも変わらない。
    ``rb status`` が nonce を全セッションへ見せる以上、``--nonce`` 単独は既定で
    **自分が所有する宣言だけ**に絞り込み、他人の宣言は ``--nonce --force`` でしか
    消せない。
    """
    board = Board(args.home)
    if args.clean:
        removed = board.remove_unreadable(reason="release --clean")
        if removed:
            _say(f"読めないファイルを {len(removed)} 件消しました")
            for path in removed:
                _say(f"  {path}")
        else:
            _say("読めないファイルはありませんでした")
        return EXIT_OK

    if args.nonce is not None:
        # **`--nonce ""` も同じ経路へ落とす。** 真偽値としての `args.nonce` で分岐すると
        # 空文字列がここを素通りし、資源 ID も指定せずに `_cmd_release` を抜けてしまう
        # （`not args.resource` の分岐にも入らない）。空の是非は `_release_by_nonce` 側で
        # 判定する。`resource` 位置引数が同時に渡されていれば、絞り込んだ宣言の資源と
        # 食い違わないかもそちらで確かめる（黙って無視しない）。
        return _release_by_nonce(board, args.nonce, force=args.force, resource=args.resource)

    if not args.resource:
        # **引数の不備を「内部エラー」にしない。** `normalize(None)` は例外になり、
        # 総括の catch-all が exit 0 と `cli_internal_error` を残す——利用者の打ち間違いが
        # 本ツールの故障として監査ログに積まれる。
        print("資源 ID を指定するか、--clean か --nonce を付けてください", file=sys.stderr)
        return EXIT_USAGE

    resource_id = naming.normalize(args.resource)
    if args.force:
        return _release_forced(board, resource_id)
    return _release_own(board, resource_id, take_all=args.all)


def _release_by_nonce(
    board: Board, prefix: str, *, force: bool = False, resource: str | None = None
) -> int:
    """``--nonce``: 資源 ID を指定せず、nonce の前方一致で 1 本だけを消す。

    **所有は既定で尊重する。** ``Board.owns`` の nonce 規則（一致すれば確実に自分の
    ものと見なす）は「nonce を知っているのはラッパーが自分で作った宣言だけ」という
    前提に乗っていた。だが ``rb status`` は今や nonce の先頭 8 桁を**全セッション**へ
    見せるので、その前提はもう成立しない。したがって前方一致した候補は、ここでいったん
    **自分が本当に所有するものだけ**（cwd / session_id）に絞ってから使う——nonce の
    一致そのものを所有の証明として使い回さない。他人の宣言にしか当たらなければ、
    「見つからない」とは区別して拒否し、``--force`` を案内する（対処が違う）。

    ``force=True`` のときだけ所有を問わない。個体指定で他人の宣言を消せる唯一の道が
    これになるので、``_release_forced`` と同じく**何を消したか必ず言う**（黙って
    他人の宣言を消すと、消された側は理由の分からない消失として体験する）。

    ``resource`` が渡されていて、絞り込んだ 1 件の資源と食い違えば、**黙って通さず**
    両方を示して拒否する。利用者は「その資源を消す」と信じて打っている。

    **空文字列（や空白だけ）は前方一致の対象にしない。** ``"".startswith("")`` は
    真になるので、弾かずに通すと ``entry.nonce.startswith("")`` が**全件に一致**し、
    曖昧判定を素通りして最初に見つかった宣言を暗黙に選んでしまう——issue #8
    「採らなかった案」が明示的に退けた「曖昧なときに暗黙で 1 件選ぶ」の再来である。
    「見つからない」（0 件一致）とは別の理由として拒否する。桁数の下限は設けない。
    1 桁でも一意に決まれば指定として成立する——短いことは問題ではなく、**空である
    ことだけ**が「何も指定していない」に当たる。
    """
    prefix = prefix.strip()
    if not prefix:
        # **「見つからない」と混ぜない。** 前者は打ち直しを誘うが、これは入力そのものが
        # 不正であり対処が違う——何を指定すればよいかが打ち直しでは分からない。
        _say("nonce が空です。前方一致させる値を指定してください", err=True)
        board.audit("release_nonce_rejected", reason="nonce が空である")
        return EXIT_USAGE

    matches = [entry for entry in board.list_all() if entry.nonce.startswith(prefix)]

    if not matches:
        _say(f"nonce '{prefix}' に一致する宣言が見つかりませんでした", err=True)
        board.audit("release_nonce_rejected", reason="一致する宣言が無い", prefix=prefix)
        return EXIT_USAGE

    if force:
        # **所有を問わない。** 絞り込みは前方一致だけで行う。
        candidates = matches
    else:
        cwd = os.getcwd()
        session_id = platform_info.session_id()
        mine = [entry for entry in matches if board.owns(entry, cwd=cwd, session_id=session_id)]
        if not mine:
            # **「無い」と「あなたのものではない」を混ぜない。** 前者は打ち直し、
            # 後者は --force を使うかどうかの判断が要る。対処が違う。
            _say(
                f"nonce '{prefix}' は自分の宣言ではありません"
                f"（{len(matches)} 件、他セッションのもの）",
                err=True,
            )
            for entry in matches:
                _say(
                    f"  nonce {entry.nonce[:8]}  {naming.display_default(entry.resource)}"
                    f"  {entry.session} / {entry.job}（since {entry.since}）",
                    err=True,
                )
            _say(f"  他セッションの宣言を消すなら rb release --nonce {prefix} --force", err=True)
            board.audit(
                "release_nonce_rejected",
                reason="自分の宣言ではない",
                prefix=prefix,
                count=len(matches),
            )
            return EXIT_USAGE
        candidates = mine

    if len(candidates) > 1:
        _say(
            f"nonce '{prefix}' が {len(candidates)} 件に一致します。"
            "もっと長い桁数を指定してください",
            err=True,
        )
        for entry in candidates:
            _say(
                f"  nonce {entry.nonce[:8]}  {naming.display_default(entry.resource)}"
                f"  {entry.session} / {entry.job}（since {entry.since}）",
                err=True,
            )
        board.audit(
            "release_nonce_rejected", reason="前方一致が曖昧", prefix=prefix, count=len(candidates)
        )
        return EXIT_USAGE

    entry = candidates[0]

    if resource is not None:
        wanted = naming.normalize(resource)
        if wanted != entry.resource:
            # **黙って通さない。** 「この資源を消す」と信じて打った利用者に、
            # 無関係な資源の宣言を消させてはならない。
            _say(
                f"食い違いがあります: 指定した資源は {naming.display_default(wanted)} ですが、"
                f"--nonce が指しているのは {naming.display_default(entry.resource)} です",
                err=True,
            )
            _say(
                f"  nonce {entry.nonce[:8]}  {entry.session} / {entry.job}（since {entry.since}）",
                err=True,
            )
            board.audit(
                "release_nonce_rejected",
                reason="resource と --nonce が食い違う",
                prefix=prefix,
                requested_resource=wanted,
                actual_resource=entry.resource,
            )
            return EXIT_USAGE

    if force:
        # **所有チェックという概念自体が要らない場面。** `remove_own` の nonce 経路は
        # 内部で「nonce が一致すれば所有とみなす」ため、既に所有確認を終えている
        # 非強制路とは違い、ここでは素の CAS（`remove_if_nonce`）を直接使う。
        removal = board.remove_if_nonce(
            entry.resource, expect_nonce=entry.nonce, reason="release --nonce --force コマンド"
        )
        if removal is RemovalResult.REMOVED:
            _say(
                f"強制解放しました: {naming.display_default(entry.resource)}"
                f"（nonce {entry.nonce[:8]}, {entry.session} / {entry.job}）"
            )
            return EXIT_OK
        if removal is RemovalResult.ABSENT:
            _say(f"宣言は既にありませんでした: nonce {entry.nonce[:8]}")
            return EXIT_OK
        if removal is RemovalResult.NOT_OWNED:
            _say("宣言が入れ替わりました（解放していません）", err=True)
            return EXIT_BUSY
        _say("警告: 宣言を取り下げられませんでした（掲示板に残っています）", err=True)
        return EXIT_BUSY

    # **所有は既に確認済み。** ここへ来る entry は必ず自分のものなので、
    # `remove_own` の nonce 経路（cwd・session_id を問わず 1 本だけを狙う）を
    # そのまま使ってよい。**`cwd` も転送する**——`remove_own` は nonce が非空なら
    # 内部の所有判定を nonce 一致だけで済ませるので通常は無くても効くが、nonce を
    # 持たない旧形式の宣言（cwd の祖先フォールバックでしか所有と判定できない）が
    # 将来ここへ来ても、掲示板に残っているのに「無い」と嘘を言わないための保険である。
    result = board.remove_own(
        entry.resource, reason="release --nonce コマンド", nonce=entry.nonce, cwd=cwd
    )
    if result.removed:
        _say(f"解放しました: {naming.display_default(entry.resource)}（nonce {entry.nonce[:8]}）")
        return EXIT_OK
    if result.swapped:
        _say("宣言が入れ替わりました（解放していません）", err=True)
        return EXIT_BUSY
    if result.failed:
        _say("警告: 宣言を取り下げられませんでした（掲示板に残っています）", err=True)
        return EXIT_BUSY
    # **ここへは通常来ない。** 所有は既に確認済みなので `remove_own` は原則
    # removed/swapped/failed のいずれかで返る。絞り込みからここまでの間に
    # 他セッションが同じ宣言を消していた場合だけ素通りする。「無かった」を
    # 「消せなかった」と混ぜない。
    _say(
        f"宣言を取り下げませんでした（既に掲示板に無い可能性）: nonce {entry.nonce[:8]}",
        err=True,
    )
    return EXIT_BUSY


def _release_forced(board: Board, resource_id: str) -> int:
    """``--force``: その資源の宣言を**所有を問わず全部**消す。

    見ないことが ``--force`` の意味である。**何件消したかは必ず言う**（黙って
    他人の宣言を消すと、消された側は理由の分からない消失として体験する）。
    """
    with board.locked(resource_id) as lock:
        _warn_if_unlocked(board, resource_id, lock, event="force_release_unlocked")
        before = board.list_for(resource_id)
        removed = board.remove_all(resource_id, reason="release コマンド（強制）")

    if not before:
        _say(f"宣言はありませんでした: {naming.display_default(resource_id)}")
        return EXIT_OK

    _say(f"強制解放しました: {naming.display_default(resource_id)}（{removed} 件）")
    for entry in before:
        _say(f"  {entry.session} / {entry.job}（since {entry.since}）")
    if removed < len(before):
        _say(
            f"警告: {len(before) - removed} 件を消せませんでした（他プロセスが読んでいる可能性）",
            err=True,
        )
    return EXIT_OK


def _release_own(board: Board, resource_id: str, *, take_all: bool = False) -> int:
    """自分の宣言を取り下げる。

    照合は :meth:`Board.owns` と同じ規則（nonce → session_id → cwd の祖先）。
    **他人のものは消さない。** 消したものは 1 件ずつ表示する——祖先フォールバックで
    誤って選んだとき、目視できなければ気づく手段が無い。

    **自分の宣言が 2 件以上あるときは、``--all`` が無ければ何も消さずに拒否する。**
    曖昧なまま 1 件を選ぶと、黙って間違った方を消すことになり、2 件とも消すより悪い
    （DESIGN.md「採らなかった案」）。1 件のときは摩擦を足さない——曖昧なときだけの保険である。
    """
    cwd = os.getcwd()
    session_id = platform_info.session_id()

    if not take_all:
        mine = [
            entry
            for entry in board.list_for(resource_id)
            if board.owns(entry, cwd=cwd, session_id=session_id)
        ]
        if len(mine) > 1:
            # **消す前に止める。** 曖昧なまま 1 件選ぶと、黙って間違った方を消すのが
            # 「2 件とも消す」より悪い（DESIGN.md「採らなかった案」）。
            _say(
                f"自分の宣言が {len(mine)} 件あります。曖昧なので何も消しません",
                err=True,
            )
            for entry in mine:
                _say(f"  nonce {entry.nonce[:8]}  {entry.job}（since {entry.since}）", err=True)
            _say("  1 本だけ消すには rb release --nonce <nonce の先頭 8 桁>", err=True)
            _say(
                f"  まとめて消すには rb release {naming.display_default(resource_id)} --all",
                err=True,
            )
            board.audit("release_ambiguous", resource=resource_id, count=len(mine))
            return EXIT_USAGE

    result = board.remove_own(resource_id, reason="release コマンド", cwd=cwd)
    if result.removed:
        _say(f"解放しました: {naming.display_default(resource_id)}（{len(result.removed)} 件）")
        for entry in result.removed:
            _say(f"  {entry.session} / {entry.job}（since {entry.since}）")

    if result.swapped:
        # **他セッションが取り直していた。** 新しい宣言は消していない（CAS が守った）。
        # 0 を返すと「解放した」と読まれるので返さない。
        _say("宣言が入れ替わりました（解放していません）", err=True)
        for entry in board.list_for(resource_id):
            _say(f"  現在: {entry.session} / {entry.job}（since {entry.since}）", err=True)
        return EXIT_BUSY

    if result.failed:
        # **消せなかったことを「無かった」と混ぜない。** 残っているのに消えたと読まれる。
        _say(
            f"警告: {len(result.failed)} 件を消せませんでした（他プロセスが読んでいる可能性）",
            err=True,
        )
        return EXIT_BUSY
    if result.removed:
        return EXIT_OK

    if not result.any_here:
        _say(f"宣言はありませんでした: {naming.display_default(resource_id)}")
        return EXIT_OK

    # **他人の宣言を「無い」と言わない。** 誰がいるかを見せて、--force を案内する。
    _say(f"自分の宣言はありません: {naming.display_default(resource_id)}", err=True)
    for entry in result.foreign:
        _say(f"  {entry.session} / {entry.job}（since {entry.since}）", err=True)
    _say(f"  他セッションの宣言を消すなら rb release {resource_id} --force", err=True)
    # **0 を返さない。** 消すつもりで打って何も消えていない。0 は「解放した」と読まれる。
    return EXIT_BUSY


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
        help=(
            "次に来る人への申し送り（例: 'VRAM 残 6GB まで空き' '15 コア占有'）。"
            "**許可を与える旗ではない**（与える保持者がいない）。本ツールは解釈しない"
        ),
    )
    parser.add_argument("--log", default=None, help="進捗が読めるログのパス")
    parser.add_argument("--display", default=None, help="表示名")
    # **承知で並ぶ意思表示。掲示板には役割として記録されない。**
    # かつての `rb join` が作っていた「相乗り」という種類の記録は無い。
    parser.add_argument(
        "--share",
        action="store_true",
        help="既に宣言がある資源へ並んで使う（誰の宣言も消さない）",
    )
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
            "自分の宣言を取り下げる。自分の宣言が 2 件以上あるときは、曖昧なので"
            "既定では何も消さずに拒否する（--all でまとめて消せる）。"
            "--nonce を使えば資源 ID 無しで 1 本だけを狙って消せる。"
            "他セッションの宣言まで消すのは --force だけである。"
        ),
    )
    release.add_argument("resource", nargs="?", help="資源 ID（--clean / --nonce のときは不要）")
    release.add_argument(
        "--nonce",
        default=None,
        help=(
            "資源 ID の代わりに nonce の前方一致で 1 本を指定する（rb status に表示される"
            "先頭 8 桁でよい）。既定では自分が所有する宣言だけに絞り込み、一意に決まらなければ"
            "何も消さず候補を挙げて拒否する。他セッションの宣言を消すには --force を併用する"
        ),
    )
    release.add_argument(
        "--all",
        action="store_true",
        help="自分の宣言が複数あっても全部まとめて解放する（曖昧さの拒否を明示的に上書きする）",
    )
    release.add_argument(
        "--force", action="store_true", help="他セッションの宣言も強制的に解放する"
    )
    release.add_argument(
        "--clean",
        action="store_true",
        help="読めないファイルを消す（どの資源のものか判別できないので資源は指定しない）",
    )
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
    update.add_argument("--sharing", default=None, help="次に来る人への申し送り")
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


def _use_utf8_for_our_own_output() -> None:
    """**自分の出力だけを UTF-8 にする。**

    本ツールの出力は日本語である。Windows の Python はコンソールのコードページで
    書くため、**日本語 Windows 以外（cp1252 等）では print が
    ``UnicodeEncodeError`` で落ちる**。argparse のエラー経路のように `_say` を通らない
    出力もあるので、例外を握るだけでは足りない。

    **`bin/rb.py` にだけ置いていたのでは足りなかった。** `uv tool install` で入る
    `rb` はエントリポイント（`resource_broker.cli:main`）を直接呼ぶので、ランチャを
    通らない。**配布経路によって挙動が変わっていた**（CI の英語 Windows で発覚）。

    **環境変数（`PYTHONUTF8`）で解決してはならない。** `rb run -- python train.py` の
    子がそれを継承し、`open()` の既定エンコーディングが変わる。それまで動いていた
    ジョブが `UnicodeDecodeError` で落ちうるし、「rb run した時だけ落ちる」ので原因に
    たどり着けない。**利用者のジョブの挙動を変えることは目的ではない。**
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 直せなくても実行は続ける
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """エントリポイント。

    内部エラーは 0 を返して通す（fail-open）。本ツールの不具合で
    ユーザーの作業を止めないことを、コード上でも保証する。
    """
    _use_utf8_for_our_own_output()
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
