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

from . import __version__, clock, liveness, naming, platform_info, runner, waiting
from .board import (
    Board,
    BoardListing,
    ConfirmedEntry,
    Entry,
    ForcedRemoval,
    LockState,
    OwnRemoval,
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

#: 内部の故障で、その操作を完了できなかったときの終了コード。
#:
#: **``EXIT_BUSY``（正常に読めた上で使用中）とも ``EXIT_OK``（完了した）とも分ける。**
#: fail-open は「資源アクセスを止めない」原則であって「走らなかった操作を成功と
#: 報告してよい」ではない（cli.py 冒頭の「終了コードの方針」）。掲示板が読めない・
#: 壊れているなど、**判定材料が無くて操作を完了できなかった**ことを表す。
#:
#: 元は ``wait`` 専用の ``EXIT_WAIT_BROKEN`` として導入した（上限到達
#: ``EXIT_BUSY`` と畳むと「上限まで待った」と「1 度も待っていない」を区別できない
#: ため）。同じ構造の故障が ``release --nonce``（掲示板の一部が読めない）にも
#: 現れたため、値 3 はそのままにこのカテゴリ全体へ一般化した——終了コードの
#: 空間が増えるほど呼び出し側の分岐が複雑になる（issue #15 #8）ので、4 つ目の
#: 値を新設せず既存の意味を広げる側を取った。
EXIT_BROKEN = 3

#: ``EXIT_BROKEN`` の旧名（``wait`` 専用だった頃の名前）。**削除しない。**
#: 値を一般化した際にシンボルごと消すと、``from resource_broker.cli import
#: EXIT_WAIT_BROKEN`` としていた Python 側の利用者を壊す（issue #17 指摘 7）。
#: 新規のコードは ``EXIT_BROKEN`` を使うこと——この名前は互換のためだけに残す。
EXIT_WAIT_BROKEN = EXIT_BROKEN

#: ``RemovalResult`` → 終了コードの対応。**この辞書だけを直す。**
#:
#: 各所で ``if result is RemovalResult.FAILED: return EXIT_BUSY`` と書けるように
#: しない——3 回のレビューで毎回「ここだけ書き忘れた」「ここだけ ``EXIT_BUSY`` へ
#: 畳んだ」が出た（issue #18 指摘 4）。削除結果を終了コードへ写す箇所は、
#: 単発の削除（``release --nonce --force``）でも複数件の集計
#: （:func:`_exit_for_own_removal` / :func:`_exit_for_forced_removal`）でも、
#: 必ずこの表を経由させる。**この表そのものをテストで固定する。**
_REMOVAL_EXIT: dict[RemovalResult, int] = {
    RemovalResult.REMOVED: EXIT_OK,
    RemovalResult.ABSENT: EXIT_OK,
    RemovalResult.NOT_OWNED: EXIT_BUSY,
    RemovalResult.FAILED: EXIT_BUSY,
    RemovalResult.UNCONFIRMED: EXIT_BROKEN,
}


def _exit_for_removal(result: RemovalResult) -> int:
    """単発の :class:`RemovalResult` を終了コードへ写す。"""
    return _REMOVAL_EXIT[result]


def _exit_for_own_removal(result: OwnRemoval) -> int:
    """:class:`OwnRemoval`（自分の宣言だけを対象にした削除）を終了コードへ写す。

    優先順位は「確認できなかった」＞「入れ替わった／消せなかった」＞「消せた」
    ＞「そもそも対象が無かった」＞「他人のものしか無かった」——1 件でも異常が
    あれば、たとえ他の 1 件が消せていても素直な成功としては返さない
    （呼び出し側が次の手順へ黙って進まないようにするため）。
    """
    if result.unconfirmed:
        return EXIT_BROKEN
    if result.swapped or result.failed:
        return EXIT_BUSY
    if result.removed:
        return EXIT_OK
    if not result.any_here:
        return EXIT_OK
    return EXIT_BUSY  # foreign のみ


def _exit_for_forced_removal(result: ForcedRemoval) -> int:
    """:class:`ForcedRemoval`（``--force``）を終了コードへ写す。

    優先順位は :func:`_exit_for_own_removal` と同じ考え方——1 件でも
    「確認できなかった」「入れ替わった」「消せなかった」があれば、
    他が消せていても素直な成功（``EXIT_OK``）としては返さない。
    """
    if result.unconfirmed or result.swapped or result.failed:
        return EXIT_BROKEN
    return EXIT_OK


#: ``--found`` の受け付ける値と、それが表す実測の結論。
FOUND_CHOICES: dict[str, bool | None] = {"busy": True, "free": False, "unknown": None}

#: 子プロセスの起動。テストで差し替える（実プロセスを起動しないため）。
SPAWN: runner.Spawn = runner.default_spawn


def _version_string() -> str:
    """``rb --version`` が出す文字列を組み立てる。

    **配布経路が 2 つある**（``uv tool install`` と Claude Code プラグイン）ため、
    版だけでは今動いている ``rb`` がどちらから来たか解けない（issue #11）。
    実行元のパッケージディレクトリを併記し、そこを解けるようにする。

    版は ``importlib.metadata`` ではなく ``__init__.py`` の ``__version__`` から
    直接読む。プラグイン経由の実行には配布メタデータが無く、``importlib.metadata`` は
    ``PackageNotFoundError`` になる（実測済み）。
    """
    package_dir = Path(__file__).resolve().parent
    return f"resource-broker {__version__}\n{package_dir}"


class _VersionAction(argparse.Action):
    """``--version``: 版と実行元のパッケージディレクトリを表示して終了する。

    標準の ``action="version"`` は使わない。文字列を ``HelpFormatter`` に通すため、
    埋め込んだ改行が空白に潰されて折り返され、Windows のパス（バックスラッシュ区切り）が
    行の途中で千切れる（実測）。ここは折り返しをかけずにそのまま出す。
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str | None = None,  # noqa: A002 - argparse.Action の引数名に合わせる
    ) -> None:
        super().__init__(
            option_strings=option_strings, dest=dest, default=default, nargs=0, help=help
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        print(_version_string())
        parser.exit()


def assess_detailed(
    board: Board, resource_id: str, observation: Observation | None = None
) -> tuple[list[tuple[Verdict, Path, Entry]], BoardListing]:
    """その資源の宣言を**1 件ずつ**判定する。**完全性つき**（:class:`BoardListing`）。

    破壊的な判断（``claim`` の幽霊退去・``--force``）はこちらを使うこと。
    ``pairs_for``（完全性を捨てる）ではなく :meth:`Board.pairs_for_detailed`
    から作るため、掲示板の一部が読めなかったかを呼び出し側が確かめられる
    ——読めなかった側に生きた宣言が隠れているかもしれないのに、それを見失った
    まま「幽霊しかいない」「空いている」と誤認する経路を閉じる（issue #18 指摘 1）。

    Parameters
    ----------
    observation : Observation, optional
        セッションが調べた結果。省略時は「調べていない」として扱う。
        **本関数は資源を調べない**。資源の種別で分岐する箇所はここに存在しない。
        実測は資源の状態であって宣言ごとの状態ではないので、**全ての宣言に同じ値を
        渡す**（「使用中」は誰か 1 人でも生きていれば真、という向きで効く）。

    Returns
    -------
    tuple of (list of (Verdict, Path, Entry), BoardListing)
        古い順の判定結果と、元になった列挙。``listing.complete`` が ``False``
        のときは判定そのものが不完全な集合に基づいている。``listing.confirmed()``
        は判定と同じ並び・同じ長さで対応するので、ゲーストを退けるときは
        ``zip`` して個体を選べる。
    """
    listing = board.pairs_for_detailed(resource_id)
    now = clock.now()
    boot = platform_info.boot_time()
    judged: list[tuple[Verdict, Path, Entry]] = []
    for path, entry in listing.pairs:
        verdict = liveness.judge(
            has_entry=True,
            since=entry.since_dt,
            boot=boot,
            observation=observation or Observation(),
            pid_alive=platform_info.pid_alive(entry.pid),
            now=now,
        )
        judged.append((verdict, path, entry))
    return judged, listing


def assess(
    board: Board, resource_id: str, observation: Observation | None = None
) -> list[tuple[Verdict, Entry]]:
    """その資源の宣言を**1 件ずつ**判定する。古い順に返す。**完全性は捨てる**（表示用）。

    **どれが先に取ったかで扱いを変えない。** 宣言は対等であり、生きているか幽霊かは
    それぞれの ``since`` / ``boot`` / PID で決まる。役割を持たせていた頃は、
    片方（主宣言）が消えるともう片方（相乗り）の意味が変わってしまい、走っている
    作業がいるのに「空き」と答える経路になっていた。

    破壊的な判断には :func:`assess_detailed` を使うこと。
    """
    judged, _ = assess_detailed(board, resource_id, observation)
    return [(verdict, entry) for verdict, _, entry in judged]


def live_declarations_detailed(
    board: Board, resource_id: str, observation: Observation | None = None
) -> tuple[list[Entry], BoardListing]:
    """幽霊と判定されなかった宣言だけを古い順に返す。**完全性つき**。"""
    judged, listing = assess_detailed(board, resource_id, observation)
    return [e for verdict, _, e in judged if not liveness.is_free(verdict)], listing


def live_declarations(
    board: Board, resource_id: str, observation: Observation | None = None
) -> list[Entry]:
    """幽霊と判定されなかった宣言だけを古い順に返す。**完全性は捨てる**（表示用）。"""
    entries, _ = live_declarations_detailed(board, resource_id, observation)
    return entries


def _known_resources(board: Board) -> list[str]:
    """掲示板に載っている資源を列挙する。"""
    return _known_resources_detailed(board)[0]


def _known_resources_detailed(board: Board) -> tuple[list[str], bool]:
    """掲示板に載っている資源と、**読めなかったものがあったか**を返す。

    読めなかったことを畳まない。畳むと「掲示板は空です」と断定してしまい、
    **実際には使われている資源を空きとして配る**（DESIGN.md「Liveness Judgment」の
    非対称性の裏返しであり、断定してよい側ではない）。
    """
    listing = board.list_all_detailed()
    resources: list[str] = []
    seen: set[str] = set()
    for entry in listing.entries:
        if entry.resource not in seen:
            seen.add(entry.resource)
            resources.append(entry.resource)
    return resources, not listing.complete


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
    """全件を表示する。**資源 ID を引数に取らない。**

    かつては資源 ID を指定できたが、``normalize()`` は大文字小文字を保持するため
    ``GPU0`` と ``gpu0`` は別の資源になり、名指しで聞くと相手の宣言が見えず
    「空き」と誤って答えていた（issue #9。実運用で `gpu0` が 7.3 時間押さえられ、
    その間 `rb status GPU0` は空きと答えた）。本ツール自身が 3 か所で「資源名を
    指定するな」と案内していたのに機能として残すのは、注意書きで防ごうとして
    いるのと同じであり、機能ごと消して全件表示だけにする。
    """
    board = Board(args.home)
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
        rows.append(
            {
                "resource": resource_id,
                # 一覧の見出しは資源 ID だけである（display による別名の併記は廃止した。
                # issue #9 — 合成した見出しと資源 ID そのものが書式で区別できなかった）。
                "label": naming.display_default(resource_id),
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

        judged, listing = assess_detailed(board, resource_id, observation)

        if not listing.complete:
            # **完全でないときは退去を一切行わない。** 読めなかった側に、いま
            # 「幽霊」と判定した宣言の生きた入れ替わり先や、追加の生きた宣言が
            # 隠れているかもしれない——不完全な候補集合から他人の宣言を消すのが
            # issue #18 指摘 1 の欠陥そのものである。``claim`` 本体の fail-open は
            # 変えない（資源アクセスは止めない）が、**退去だけは止める**。
            notices.append(
                "[rb] 掲示板の一部を読めませんでした。**退去は行わず**続行します"
                "（生きた宣言を見逃している可能性があります）"
            )
            board.audit(
                "claim_unconfirmed",
                resource=resource_id,
                reason="掲示板の一部が読めない",
                force=force,
            )
            living = [e for verdict, _, e in judged if not liveness.is_free(verdict)]
        else:
            # **幽霊は退ける。** 根拠は 3 つのいずれかで、判定は 1 件ずつ独立している。
            # 選択に使った実体（``listing.confirmed()``）をそのまま渡すので、
            # 削除の直前で完全性の情報を捨て直さない（issue #17 指摘 2 と対）。
            selections = listing.confirmed()
            for (verdict, _path, entry), selection in zip(judged, selections, strict=True):
                if not liveness.is_free(verdict):
                    continue
                reason = "強制取得" if force else "幽霊と判定した"
                # **force=True で渡す。** ここは既に `assess_detailed` が幽霊と判定した
                # 個体を、完全性を確認した列挙からそのまま消す場面であり、``--force``
                # と同じ「個体を選ぶ責任は呼び出し側が持ち、CAS は実体の入れ替わりだけを
                # 見る」形である。nonce を持たない旧形式の幽霊だけを除外すると、それだけ
                # が退去されずに残り続け、幽霊判定が資源ごとに不揃いになる。
                removal = board.remove_confirmed(selection, reason=reason, force=True)
                if removal not in (RemovalResult.REMOVED, RemovalResult.ABSENT):
                    # **消せなかったことを「使用中」に化けさせない。** 掲示板のファイルを
                    # 消せなかっただけで、資源の保持者を確認したわけではない。平坦化で
                    # 幽霊のファイルは何も塞がなくなった（名前が nonce なので新しい宣言は
                    # 作れる）ので、退去に失敗しても取得を通してよい。fail-open は
                    # 「本ツール側の故障で作業を止めない」ことである。
                    notices.append(_explain_failed_displacement(removal))

            # **退去のあとに読み直す。** 幽霊を消している間に他セッションが入っていることが
            # ある（実際、同じ幽霊を見た 2 人のうち一方が先に取り直す）。古い読み取りで
            # 判断すると、**生きた宣言があるのに気づかずに通す**。この再読み取りも
            # 完全とは限らないので、詳細版で確かめる。
            living, living_listing = live_declarations_detailed(board, resource_id, observation)

            if not living_listing.complete:
                notices.append(
                    "[rb] 掲示板の一部を読めませんでした。**追加の退去は行わず**続行します"
                )
                board.audit(
                    "claim_unconfirmed",
                    resource=resource_id,
                    reason="退去後の再読み取りで一部が読めない",
                    force=force,
                )
            elif force:
                living_selections = {
                    selection.entry.nonce: selection for selection in living_listing.confirmed()
                }
                for entry in list(living):
                    selection = living_selections.get(entry.nonce)
                    if selection is None:
                        continue  # 理論上起きない（living は living_listing.pairs の部分集合）
                    removal = board.remove_confirmed(selection, reason="強制取得", force=True)
                    if removal is RemovalResult.REMOVED:
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
            label = naming.display_default(resource_id)
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
    if removal is RemovalResult.UNCONFIRMED:
        return (
            "退けようとした宣言の消去を確認できませんでした"
            "（掲示板の一部が読めません。監査ログを参照）"
        )
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
        log=args.log,
        force=args.force,
        share=args.share,
    )
    if result.entry is None:
        return result.code

    if not result.declared:
        _warn_not_declared()
        return result.code

    print(f"宣言しました: {naming.display_default(result.entry.resource)} / {result.entry.job}")
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

    **消すのは自分が出した 1 件だけ。** ``entry`` は呼び出し側が :func:`acquire`
    で自分自身が書いた宣言そのものであり、**列挙を一切経由しない**——
    :meth:`Board.confirm_own_declaration` でそのまま削除できる選択にする。
    以前は :meth:`Board.remove_own` へ ``nonce`` だけを渡していたため、内部で
    ``pairs_for`` によるこの資源全体の再列挙が起きていた。自分が直接書いた
    実体の居場所は最初から分かっているので、その再列挙そのものが不要である
    （issue #18 指摘 2）。

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

        selection = board.confirm_own_declaration(entry)
        result = board.remove_confirmed(selection, reason=reason)
        if result is RemovalResult.REMOVED:
            _say(f"解放しました: {naming.display_default(resource_id)}")
        elif result is RemovalResult.NOT_OWNED:
            _say("宣言が入れ替わりました（解放していません）", err=True)
        elif result is RemovalResult.UNCONFIRMED:
            _say(
                "警告: 宣言を取り下げられたか確認できませんでした"
                "（削除直後に掲示板の一部が読めなくなりました）",
                err=True,
            )
        elif result is RemovalResult.FAILED:
            _say(
                "警告: 宣言を取り下げられませんでした（掲示板に残っています）",
                err=True,
            )
        else:
            # **「消さなかった」を「消せなかった」と混ぜない。** 走行中に外部から
            # 消えている場合（``--force``、再起動掃除）に「掲示板に残っています」と
            # 出すと嘘になる。
            _say(
                "宣言を取り下げませんでした（既に掲示板にありません）",
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
    #
    # **完全に読めたときだけ、この近道を取る。** 読めなかった側に生きた宣言が
    # 隠れているかもしれないのに「既に解放されています」と積極的な成功表現を
    # 返していた（issue #17 指摘 4）。読めなければ近道を諦めて素直に待機へ入る
    # ——``wait_for_room`` 自身が「読めないポーリング」を「解放済み」に取り違えない
    # ようになっているので、待機に入れば誤りは起きない。
    keys, complete = waiting.holder_keys_detailed(board, resource_id)
    if complete and not keys:
        print(f"既に解放されています: {naming.display_default(resource_id)}")
        return EXIT_OK

    entry = first_declaration(board, resource_id)
    if entry is None:
        print(f"待機します: {naming.display_default(resource_id)}")
    else:
        print(
            f"待機します: {naming.display_default(resource_id)} <- {entry.session} / {entry.job}"
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

    if result.reason == waiting.BROKEN:
        # **一度も掲示板を完全に読めないまま上限に達した。** `TIMEOUT`（正常に
        # 読めた上でまだ使用中）とは違い、使用中かどうかの確認そのものが取れて
        # いない。ここで `EXIT_BUSY` を返すと「確認した上で使用中」と偽ることに
        # なるので、`EXIT_BROKEN` で区別する（issue #17 指摘 4）。
        print(
            f"掲示板を読めないまま上限に達しました（{result.polls} 回確認"
            f" / {result.waited_s:.0f} 秒）。使用中かどうかは未確認です",
            file=sys.stderr,
        )
        print(WAIT_ADVICE, file=sys.stderr)
        return EXIT_BROKEN

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


def _nonce_key(record: dict) -> tuple[str, str] | None:
    """nonce による厳密な対応付けの鍵。**両方が nonce を持つときだけ**使う。

    nonce は宣言ごとに一意なので、資源 + nonce が一致すれば確実に同じ宣言である
    ——job だけを鍵にすると、``--nonce`` で片方だけを消したときに、時刻順で先に
    来た**別の宣言**（まだ生きているほう）に解放が結び付いてしまう（「消していない
    方が消えたことになり、消した方には解放の記録が無い」という嘘を履歴が語る）。
    """
    resource = str(record.get("resource", ""))
    nonce = record.get("nonce")
    if isinstance(nonce, str) and nonce:
        return (resource, nonce)
    return None


def _job_key(record: dict) -> tuple[str, str]:
    """資源 + job のフォールバック鍵。nonce を持たない側だけが使う。"""
    return (str(record.get("resource", "")), str(record.get("job", "")))


def _match_releases(claims: list[dict], removals: list[dict]) -> list[tuple[dict | None, bool]]:
    """時刻順に並んだ宣言へ、その直後の解放を 1 件ずつ対応させる。

    **2 段階で対応付ける。**

    1. **nonce が両方にあるもの同士を、厳密に**（資源 + nonce の一致）対応付ける。
    2. **step 1 で残った未使用のレコードだけを**、従来どおり資源 + job ＋時刻順で
       対応付ける。

    2 段に分ける理由は、ローリング更新で新旧の監査ログが混在すると**単純な
    1 種類の鍵では絶対に一致しない**ためである。古い ``claimed``（nonce 無し、
    旧バージョンの書き込み）と新しい ``removed``（nonce 有り）が同じ宣言に
    属していても、旧側は資源+job の鍵、新側は資源+nonce の鍵になり、鍵の種類が
    違う以上どんな実装でも一致しえない。**片方が nonce を持たない対応付けは
    全て step 2（時刻順のフォールバック）を経由させる**ことで、鍵の種類の違いに
    関わらず対応が取れるようにする。

    **step 2 は「少なくとも片側が nonce を欠く」組にしか対応付けない。** 両側が
    nonce を持ちながら step 1 で一致しなかった組は、**「不確実」ではなく
    「別個体だと確定している」**——nonce は宣言ごとに一意なので、値が違う
    2 つの記録が同じ宣言を指すことはあり得ない。ここを time+job の近さだけで
    対応付けると、無関係な 2 つの記録を「不確実な対応」として結びつけてしまう
    （issue #18 指摘 6）。**nonce 付きの ``removed`` は nonce 単位で重複排除する**
    ——監査ログの重複行が、対応しないはずの claim に誤って割り当てられるのを防ぐ。

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
    list of (dict or None, bool)
        ``claims`` と同じ並び・同じ長さ。2 つ目の要素は**この対応付けが
        不確実か**——nonce の厳密な一致ではなく、資源 + job のフォールバックで
        決めたときに ``True`` になる（片方が旧形式で nonce を持たない場合を含む）。
        対応が付かなかった（``None``）ときは ``False``（不確実さを論じる対象が無い）。
    """
    # --- step 1: nonce が両方にあるもの同士を厳密に対応付ける ---------------------
    removals_by_nonce: dict[tuple[str, str], dict] = {}
    for record in removals:
        key = _nonce_key(record)
        if key is not None and key not in removals_by_nonce:
            # 同じ nonce の removed が複数あることは想定しないが、あれば最初の
            # 1 件を採る（監査ログの重複・再送があっても対応付けを壊さないため）。
            removals_by_nonce[key] = record

    matched: list[dict | None] = [None] * len(claims)
    uncertain: list[bool] = [False] * len(claims)
    used_by_id: set[int] = set()
    remaining_claim_indices: list[int] = []

    for index, claim in enumerate(claims):
        key = _nonce_key(claim)
        release = removals_by_nonce.get(key) if key is not None else None
        if release is not None:
            matched[index] = release
            used_by_id.add(id(release))
        else:
            remaining_claim_indices.append(index)

    # --- step 2: 残りを資源 + job ＋ 時刻順でフォールバック対応付けする -------------
    # **nonce 付き removed は nonce 単位で重複排除する。** 同じ nonce の記録が
    # 複数残っていると（監査ログの重複・再送）、両方が step 2 の候補に入り、
    # 別々の claim にそれぞれ「対応した」ことにされてしまう。
    remaining_removals: list[dict] = []
    seen_removal_nonces: set[str] = set()
    for record in removals:
        if id(record) in used_by_id:
            continue
        key = _nonce_key(record)
        if key is not None:
            if key in seen_removal_nonces:
                continue
            seen_removal_nonces.add(key)
        remaining_removals.append(record)

    Key = tuple[str, str]
    pending: dict[Key, list[tuple[datetime, dict]]] = {}
    for record in remaining_removals:
        when = _parse_at(record.get("at"))
        if when is None:
            continue
        pending.setdefault(_job_key(record), []).append((when, record))
    for items in pending.values():
        items.sort(key=lambda item: item[0])

    # 鍵ごとに「どこを使ったか」を集合で持つ。**片側が nonce を持ちながら
    # 消費せずに飛ばした候補**（別個体だと確定しているので対応させない）を、
    # 別の claim（nonce を持たない側）がまだ使える必要があるため、
    # 「ここまで使った」という単純な位置ポインタでは表現できない。
    used_positions: dict[Key, set[int]] = {}
    for index in remaining_claim_indices:
        claim = claims[index]
        started = _parse_at(claim.get("at"))
        claim_has_nonce = _nonce_key(claim) is not None
        key = _job_key(claim)
        items = pending.get(key, [])
        taken = used_positions.setdefault(key, set())
        for pos, (when, record) in enumerate(items):
            if pos in taken:
                continue
            if started is not None and when < started:
                continue
            if claim_has_nonce and _nonce_key(record) is not None:
                # **両側に nonce があり、ここまでで一致しなかった。**
                # 「不確実」ではなく別個体だと確定している——対応付けない
                # （消費もしない。他の claim がこの候補を必要とするかもしれない）。
                continue
            matched[index] = record
            uncertain[index] = True
            taken.add(pos)
            break

    return list(zip(matched, uncertain, strict=True))


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
        for record, (release, uncertain) in zip(records, releases, strict=True):
            elapsed, stated = _elapsed_and_stated(record, release)
            claims.append(
                {
                    **record,
                    "released_at": release.get("at") if release else None,
                    "release_reason": release.get("reason") if release else None,
                    "elapsed_seconds": elapsed,
                    "stated_seconds": stated,
                    # **片側が nonce を持たない対応付けだと表明する。** nonce の
                    # 厳密な一致ではなく資源 + job ＋時刻順のフォールバックで決めた
                    # ペアであり、新旧の監査ログが混在する窓では**別の宣言を誤って
                    # 対応付けている可能性がある**（issue #17 指摘 5）。
                    "pairing_uncertain": uncertain,
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
    for record, (release, _uncertain) in zip(records, releases, strict=True):
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

    **「宣言が無い」と「確認できない」を混同しない。** 以前は ``list_for``
    （読めなかったものを黙って飛ばす）で数えていたため、不正な UTF-8 など
    掲示板の一部が読めない場合でも「0 件」と断定し ``EXIT_USAGE``（利用者の
    入力ミス）を返していた。``update`` は fail-open なコマンドなので、
    読めなかった側に自分の宣言が隠れているかもしれないなら、それは入力ミス
    ではなく「確認できなかった」であり、作業は止めずに ``EXIT_OK`` で通す
    （issue #18 指摘 8。今回の UTF-8 修正が作った退行）。
    """
    cwd = os.getcwd()
    session_id = platform_info.session_id()
    listing = board.pairs_for_detailed(resource_id)
    declarations = listing.entries
    mine = [e for e in declarations if board.owns(e, cwd=cwd, session_id=session_id)]

    if not declarations:
        if not listing.complete:
            print(
                "掲示板の一部を読めず、宣言の有無を確認できませんでした（更新は行っていません）",
                file=sys.stderr,
            )
            board.audit(
                "update_unconfirmed", resource=resource_id, reason="掲示板の一部が読めない"
            )
            return EXIT_OK
        print("宣言が見つかりませんでした", file=sys.stderr)
        return EXIT_USAGE

    if not listing.complete:
        # **候補は見つかったが、これで全部とは限らない。** fail-open なので
        # 止めはしないが、見えていない自分の宣言が別にあるかもしれないことは
        # 伝える。
        _say(
            "注意: 掲示板の一部を読めませんでした（他に自分の宣言があるかもしれません）",
            err=True,
        )

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

    print(f"更新しました: {naming.display_default(entry.resource)} / {entry.job}")
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
        # **走査が不完全なら成功と言わない。** 以前は「消せた件数」しか見ていな
        # かったため、掲示板ディレクトリ自体が読めなかった（権限・切断された
        # ネットワークパス等）場合でも「読めないファイルはありませんでした」と
        # 積極的な成功表現を返し、常に ``EXIT_OK`` だった（issue #18 指摘 5。
        # 新規テストがこの誤った意味を仕様として固定していた）。
        result = board.remove_unreadable(reason="release --clean")
        if result.removed:
            _say(f"読めないファイルを {len(result.removed)} 件消しました")
            for path in result.removed:
                _say(f"  {path}")
        elif result.complete:
            _say("読めないファイルはありませんでした")
        else:
            _say(
                "掲示板を完全に走査できませんでした（他に読めないファイルがあるかもしれません）",
                err=True,
            )
        if result.failed:
            _say(
                f"警告: {len(result.failed)} 件を消せませんでした（他プロセスが読んでいる可能性）",
                err=True,
            )
        if not result.complete or result.failed:
            return EXIT_BROKEN
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

    **一意性は、所有で絞る前の生の一致件数で判定する。** 先に「自分が本当に所有する
    ものだけ」（cwd / session_id）へ絞ってから一意性を見ると、prefix が「自分 1 件＋
    他人 1 件」に当たったとき、自分の 1 件だけが残って「一意」に見え、利用者が
    他人側を指して打っていても検討にすら上らないまま自分の宣言が黙って消える。
    しかも拒否できないので「``--force`` を使え」という案内も出せない——``--force``
    にすれば今度は 2 件とも所有を問わず候補に入り、改めて曖昧で拒否されるので、
    案内していた道自体が通らない。だから**2 件以上に当たった時点で、所有に関係なく
    曖昧として拒否する**。所有判定は、一意に決まった 1 件についてだけ行う。

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

    **掲示板の一部が読めないときは、「見つからない」も「一意」も確定させない。**
    ``list_all`` は読めなかったものを黙って飛ばすので使わない——掲示板全体が読めない
    ときも ``matches == []`` になり「見つからない」と断定してしまうし、部分的に
    読めた場合は隠れた候補があっても「一意」と誤認して削除まで進んでしまう
    （このプロジェクトは同種の欠陥を 2 度直している。``c7debaf`` / ``f662e01``）。
    ``list_all_detailed`` で読めなかった事実を受け取り、そのときは削除まで進まず
    ``EXIT_BROKEN`` で「解放は未確認」だと明示する。**``EXIT_OK`` は使わない。**
    「解放は未確認」で 0 を返すのは、走らなかった操作を成功と報告する形そのもの
    であり、cli.py 冒頭の「終了コードで嘘をつかない」に反する
    （``rb release --nonce X && 次の手順`` のように使われれば、呼び出し側は
    解放されていないまま次へ進む）。``EXIT_USAGE`` は利用者の入力ミスの意味であって
    掲示板の破損の表現ではなく、``EXIT_BUSY`` は「掲示板が正常に読めた上で使用中と
    判定できた」ときだけに使う（cli.py 冒頭の「終了コードの方針」）。``EXIT_BROKEN``
    は「内部の故障で操作を完了できなかった」という、このどちらでもない第 3 の
    カテゴリで、``wait`` の内部エラーと同じ値・同じ意味を共有する。
    """
    prefix = prefix.strip()
    if not prefix:
        # **「見つからない」と混ぜない。** 前者は打ち直しを誘うが、これは入力そのものが
        # 不正であり対処が違う——何を指定すればよいかが打ち直しでは分からない。
        _say("nonce が空です。前方一致させる値を指定してください", err=True)
        board.audit("release_nonce_rejected", reason="nonce が空である")
        return EXIT_USAGE

    listing = board.list_all_detailed()
    if not listing.complete:
        _say(
            "掲示板の一部を読めませんでした。解放は未確認です"
            "（読めなかった側に一致する宣言が隠れているかもしれません）",
            err=True,
        )
        board.audit("release_nonce_unconfirmed", reason="掲示板の一部が読めない", prefix=prefix)
        return EXIT_BROKEN

    matches = [(path, entry) for path, entry in listing.pairs if entry.nonce.startswith(prefix)]

    if not matches:
        _say(f"nonce '{prefix}' に一致する宣言が見つかりませんでした", err=True)
        board.audit("release_nonce_rejected", reason="一致する宣言が無い", prefix=prefix)
        return EXIT_USAGE

    if len(matches) > 1:
        # **一意性は所有で絞る前に見る。** 所有で絞ってから一意性を見ると、
        # 「自分 1 件＋他人 1 件」を自分の 1 件だけの「一意」だと誤認し、
        # 狙った他人の宣言ではなく自分の宣言が黙って消える（docstring 参照）。
        _say(
            f"nonce '{prefix}' が {len(matches)} 件に一致します。もっと長い桁数を指定してください",
            err=True,
        )
        for _, entry in matches:
            _say(
                f"  nonce {entry.nonce[:8]}  {naming.display_default(entry.resource)}"
                f"  {entry.session} / {entry.job}（since {entry.since}）",
                err=True,
            )
        board.audit(
            "release_nonce_rejected", reason="前方一致が曖昧", prefix=prefix, count=len(matches)
        )
        return EXIT_USAGE

    path, entry = matches[0]
    cwd = os.getcwd()

    # **一意に決まった 1 件についてだけ、所有を確かめる。** force のときだけ問わない。
    if not force:
        session_id = platform_info.session_id()
        if not board.owns(entry, cwd=cwd, session_id=session_id):
            # **「無い」と「あなたのものではない」を混ぜない。** 前者は打ち直し、
            # 後者は --force を使うかどうかの判断が要る。対処が違う。
            _say(
                f"nonce '{prefix}' は自分の宣言ではありません（{entry.session} / {entry.job}）",
                err=True,
            )
            _say(f"  他セッションの宣言を消すなら rb release --nonce {prefix} --force", err=True)
            board.audit(
                "release_nonce_rejected",
                reason="自分の宣言ではない",
                prefix=prefix,
                count=1,
            )
            return EXIT_USAGE

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

    # **選択に使った実体を、そのまま削除できる型にする。** ここまでで完全性は
    # 確認済み（listing.complete）なので :meth:`BoardListing.confirmed` は
    # 例外を出さない。
    selection = ConfirmedEntry(path=path, entry=entry)

    if force:
        # **所有チェックという概念自体が要らない場面。** 既に所有確認を終えている
        # 非強制路とは違い、ここでは公開の削除入口（`remove_confirmed`）を
        # 選択した実体に対して直接使う——再列挙しないので、選択時に確認した
        # 完全性を削除の直前で捨て直さない（issue #17 指摘 2・3）。
        removal = board.remove_confirmed(
            selection, reason="release --nonce --force コマンド", force=True
        )
        if removal is RemovalResult.REMOVED:
            _say(
                f"強制解放しました: {naming.display_default(entry.resource)}"
                f"（nonce {entry.nonce[:8]}, {entry.session} / {entry.job}）"
            )
            return _exit_for_removal(removal)
        if removal is RemovalResult.ABSENT:
            _say(f"宣言は既にありませんでした: nonce {entry.nonce[:8]}")
            return _exit_for_removal(removal)
        if removal is RemovalResult.NOT_OWNED:
            _say("宣言が入れ替わりました（解放していません）", err=True)
            return _exit_for_removal(removal)
        if removal is RemovalResult.UNCONFIRMED:
            _say(
                "警告: 宣言を取り下げられたか確認できませんでした"
                "（削除直後に掲示板の一部が読めなくなりました）",
                err=True,
            )
            return _exit_for_removal(removal)
        _say("警告: 宣言を取り下げられませんでした（掲示板に残っています）", err=True)
        return _exit_for_removal(removal)

    # **所有は既に確認済み。** ここへ来る entry は必ず自分のものなので、
    # `remove_own` の nonce 経路（cwd・session_id を問わず 1 本だけを狙う）を
    # そのまま使ってよい。**`cwd` も転送する**——`remove_own` は nonce が非空なら
    # 内部の所有判定を nonce 一致だけで済ませるので通常は無くても効くが、nonce を
    # 持たない旧形式の宣言（cwd の祖先フォールバックでしか所有と判定できない）が
    # 将来ここへ来ても、掲示板に残っているのに「無い」と嘘を言わないための保険である。
    #
    # **`declared` に、完全性を確認した ``listing`` から絞った分をそのまま渡す。**
    # 渡さないと `remove_own` が内部で再列挙し、ここまでで確認した完全性を
    # 削除の直前で捨て直す（issue #17 指摘 2・3）。
    declared_for_resource = [s for s in listing.confirmed() if s.entry.resource == entry.resource]
    result = board.remove_own(
        entry.resource,
        reason="release --nonce コマンド",
        nonce=entry.nonce,
        cwd=cwd,
        declared=declared_for_resource,
    )
    if result.removed:
        _say(f"解放しました: {naming.display_default(entry.resource)}（nonce {entry.nonce[:8]}）")
        return _exit_for_own_removal(result)
    if result.unconfirmed:
        _say(
            "警告: 宣言を取り下げられたか確認できませんでした"
            "（削除直後に掲示板の一部が読めなくなりました）",
            err=True,
        )
        return _exit_for_own_removal(result)
    if result.swapped:
        _say("宣言が入れ替わりました（解放していません）", err=True)
        return _exit_for_own_removal(result)
    if result.failed:
        _say("警告: 宣言を取り下げられませんでした（掲示板に残っています）", err=True)
        return _exit_for_own_removal(result)
    # **ここへは通常来ない。** 所有は既に確認済みなので `remove_own` は原則
    # removed/unconfirmed/swapped/failed のいずれかで返る。絞り込みからここまでの
    # 間に他セッションが同じ宣言を消していた場合だけ素通りする。「無かった」を
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

    **掲示板の一部が読めないときは、消さずに拒否する。** 「全部消す」と言っている
    以上、読めなかった側にこの資源の宣言が隠れていれば、それは消さずに掲示板へ
    残る——``--force`` は「見ないことがその意味」であって「見えなかったものは
    無かったことにする」ではない。読めない宣言が残ったまま「強制解放しました」と
    言ってはならない（issue #17 指摘 2）。

    **表示と削除を同じ列挙から作る。** 以前は表示用に ``pairs_for_detailed`` で
    列挙したあと、削除は ``Board.remove_all`` が内部で ``pairs_for`` により
    **別に列挙し直して**いたため、表示した対象と実際に消した対象が食い違いうる
    経路が残っていた（issue #15 指摘 12・issue #18 末尾）。ここでは
    :meth:`BoardListing.confirmed` で得た**同じ並び**をそのまま
    :meth:`Board.remove_selected` へ渡す——資源名だけで何件消えるか決まる
    公開入口はもう存在しない。

    **消せなかった・確認できなかった・入れ替わっていたものがあれば
    ``EXIT_BROKEN``。** 共有違反などの I/O 失敗で一部が消せなかった場合も、
    以前は ``EXIT_OK`` を返していた——警告は出すが終了コードは「成功」のままで、
    ``release --force && 次の手順`` は消えていない宣言を無視して進む。
    **終了コードで嘘をつかない**（cli.py 冒頭）。
    """
    listing = board.pairs_for_detailed(resource_id)
    if not listing.complete:
        _say(
            "掲示板の一部を読めませんでした。強制解放は未確認です"
            "（読めなかった側にこの資源の宣言が隠れているかもしれません）",
            err=True,
        )
        board.audit(
            "force_release_unconfirmed", resource=resource_id, reason="掲示板の一部が読めない"
        )
        return EXIT_BROKEN
    before = listing.entries
    # **ロックは `remove_selected` 自身が取る。** ここで外側から重ねて取ると、
    # 同一プロセス内の入れ子ロックになり `LOCK_WAIT_S` を無駄に待つ
    # （`remove_own` と同じ「列挙は外・削除は内」の形にそろえる）。
    result = board.remove_selected(
        resource_id, listing.confirmed(), reason="release コマンド（強制）"
    )

    if not before:
        _say(f"宣言はありませんでした: {naming.display_default(resource_id)}")
        return EXIT_OK

    _say(f"強制解放しました: {naming.display_default(resource_id)}（{len(result.removed)} 件）")
    for entry in result.removed:
        _say(f"  {entry.session} / {entry.job}（since {entry.since}）")
    if result.failed:
        _say(
            f"警告: {len(result.failed)} 件を消せませんでした（他プロセスが読んでいる可能性）",
            err=True,
        )
    if result.unconfirmed:
        _say(
            f"警告: {len(result.unconfirmed)} 件は消せたか確認できませんでした"
            "（削除直後に掲示板の一部が読めなくなりました）",
            err=True,
        )
    if result.swapped:
        _say(
            f"警告: {len(result.swapped)} 件は他セッションが取り直していたため消していません",
            err=True,
        )
    return _exit_for_forced_removal(result)


def _release_own(board: Board, resource_id: str, *, take_all: bool = False) -> int:
    """自分の宣言を取り下げる。

    照合は :meth:`Board.owns` と同じ規則（nonce → session_id → cwd の祖先）。
    **他人のものは消さない。** 消したものは 1 件ずつ表示する——祖先フォールバックで
    誤って選んだとき、目視できなければ気づく手段が無い。

    **自分の宣言が 2 件以上あるときは、``--all`` が無ければ何も消さずに拒否する。**
    曖昧なまま 1 件を選ぶと、黙って間違った方を消すことになり、2 件とも消すより悪い
    （DESIGN.md「採らなかった案」）。1 件のときは摩擦を足さない——曖昧なときだけの保険である。

    **数える区間と消す区間の間には TOCTOU の窓がある。** ここで数えた「1 件」を
    そのまま ``remove_own`` の無条件削除（nonce 無し）へ渡すと、数えてから呼ぶ
    までの間に 2 件目が現れたとき ``remove_own`` は**その時点で自分のもの全部**を
    読み直して消す——「消す前に止める」がまさに防ごうとした事故が、競合下でそのまま
    成立する。数える区間と消す区間を 1 つの ``Board.locked`` に寄せる案も検討したが、
    取得（``declare``）はロックが取れなくても続行する（fail-open。DESIGN.md
    「Per-Resource Lock」）ため、ロックで囲っても「ロックを尊重しない書き手」が
    割り込む窓は閉じない。窓を閉じる手段は対象を**個体（nonce）で固定する**ことだけ
    であり、CAS（:meth:`Board.remove_confirmed`）は「読んだ宣言以外を消さない」を
    ロックの有無に関係なく保証する。だから 1 件のときはその nonce を渡し、
    0 件のときは削除処理そのものへ進まない（同じ理由——その間に現れた宣言を
    「数えた 1 件」と取り違えて消しうる）。

    **掲示板の一部が読めないときは、0 件・1 件のどちらとも断定しない。**
    以前は ``list_for``（読めなかったものを黙って飛ばす）で数えていたので、
    掲示板の一部が読めなくても「0 件」「1 件」と断定して消す・消さないを決めて
    いた——読めなかった側に自分の宣言がもう 1 件隠れているかもしれない
    （issue #17 指摘 3）。:meth:`Board.pairs_for_detailed` で完全性を確認し、
    ``--all`` を含めて全ての経路で読めなかったら ``EXIT_BROKEN`` で「解放は
    未確認」と明示する。選択で得た ``(Path, Entry)`` は ``remove_own`` へ
    そのまま渡し、削除の直前で完全性の情報を捨て直さない。
    """
    cwd = os.getcwd()
    session_id = platform_info.session_id()

    listing = board.pairs_for_detailed(resource_id)
    if not listing.complete:
        _say(
            "掲示板の一部を読めませんでした。解放は未確認です"
            "（読めなかった側に自分の宣言が隠れているかもしれません）",
            err=True,
        )
        board.audit(
            "release_unconfirmed",
            resource=resource_id,
            reason="掲示板の一部が読めない",
            all=take_all,
        )
        return EXIT_BROKEN
    declared = listing.confirmed()

    nonce: str | None = None
    if not take_all:
        mine = [
            selection
            for selection in declared
            if board.owns(selection.entry, cwd=cwd, session_id=session_id)
        ]
        if len(mine) > 1:
            # **消す前に止める。** 曖昧なまま 1 件選ぶと、黙って間違った方を消すのが
            # 「2 件とも消す」より悪い（DESIGN.md「採らなかった案」）。
            _say(
                f"自分の宣言が {len(mine)} 件あります。曖昧なので何も消しません",
                err=True,
            )
            for selection in mine:
                entry = selection.entry
                _say(f"  nonce {entry.nonce[:8]}  {entry.job}（since {entry.since}）", err=True)
            _say("  1 本だけ消すには rb release --nonce <nonce の先頭 8 桁>", err=True)
            _say(
                f"  まとめて消すには rb release {naming.display_default(resource_id)} --all",
                err=True,
            )
            board.audit("release_ambiguous", resource=resource_id, count=len(mine))
            return EXIT_USAGE
        if not mine:
            # **0 件のときは削除処理へ進まない。** ここで `remove_own` を nonce 無しで
            # 呼ぶと、数えてから呼ぶまでの間に現れた「新しい自分の宣言」——数えた
            # 時点にはまだ存在しなかったもの——まで拾って消してしまう。この関数の
            # 役目は「いま数えた自分の宣言を消す」ことであって、「これから現れるかも
            # しれない宣言まで待ち構えて消す」ことではない。
            foreign = [selection.entry for selection in declared]
            if not foreign:
                _say(f"宣言はありませんでした: {naming.display_default(resource_id)}")
                return EXIT_OK
            _say(f"自分の宣言はありません: {naming.display_default(resource_id)}", err=True)
            for entry in foreign:
                _say(f"  {entry.session} / {entry.job}（since {entry.since}）", err=True)
            _say(f"  他セッションの宣言を消すなら rb release {resource_id} --force", err=True)
            return EXIT_BUSY
        # **1 件だけなら nonce を固定して渡す。** 数えたあとに 2 件目が現れても、
        # `remove_own` は渡した nonce と一致する宣言だけを対象にする（CAS）ので、
        # 新しく現れた宣言には触れない——ここが「消す前に止める」の実体である。
        nonce = mine[0].entry.nonce

    # **選択に使った `declared` をそのまま渡す。** `remove_own` が内部で再列挙すると、
    # ここまでで確認した完全性を削除の直前で捨て直すことになる（issue #17 指摘 2・3）。
    result = board.remove_own(
        resource_id, reason="release コマンド", cwd=cwd, nonce=nonce, declared=declared
    )
    if result.removed:
        _say(f"解放しました: {naming.display_default(resource_id)}（{len(result.removed)} 件）")
        for entry in result.removed:
            _say(f"  {entry.session} / {entry.job}（since {entry.since}）")

    if result.unconfirmed:
        # **消せたか確認できなかった。** 「入れ替わった」「消せなかった」とも違う
        # ——削除直後の再確認で掲示板の一部が読めなかった（issue #18 指摘 4）。
        _say(
            f"警告: {len(result.unconfirmed)} 件は消せたか確認できませんでした"
            "（削除直後に掲示板の一部が読めなくなりました）",
            err=True,
        )

    if result.swapped:
        # **他セッションが取り直していた。** 新しい宣言は消していない（CAS が守った）。
        # 0 を返すと「解放した」と読まれるので返さない。
        _say("宣言が入れ替わりました（解放していません）", err=True)
        for entry in board.list_for(resource_id):
            _say(f"  現在: {entry.session} / {entry.job}（since {entry.since}）", err=True)

    if result.failed:
        # **消せなかったことを「無かった」と混ぜない。** 残っているのに消えたと読まれる。
        _say(
            f"警告: {len(result.failed)} 件を消せませんでした（他プロセスが読んでいる可能性）",
            err=True,
        )

    only_foreign = not (result.removed or result.unconfirmed or result.swapped or result.failed)
    if result.foreign and only_foreign:
        # **他人の宣言を「無い」と言わない。** 誰がいるかを見せて、--force を案内する。
        _say(f"自分の宣言はありません: {naming.display_default(resource_id)}", err=True)
        for entry in result.foreign:
            _say(f"  {entry.session} / {entry.job}（since {entry.since}）", err=True)
        _say(f"  他セッションの宣言を消すなら rb release {resource_id} --force", err=True)

    if result.any_here:
        return _exit_for_own_removal(result)

    _say(f"宣言はありませんでした: {naming.display_default(resource_id)}")
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
        help=(
            "次に来る人への申し送り（例: 'VRAM 残 6GB まで空き' '15 コア占有'）。"
            "**許可を与える旗ではない**（与える保持者がいない）。本ツールは解釈しない"
        ),
    )
    parser.add_argument("--log", default=None, help="進捗が読めるログのパス")
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
    # **サブコマンドより前に処理させる。** ``_VersionAction`` は出会った時点で即座に
    # 表示して終了する（``nargs=0`` + ``parser.exit()``）ため、``required=True`` の
    # サブパーサ検査には到達しない——``rb --version`` がサブコマンド無しで動く理由は
    # ここにある。版だけを答えるので、掲示板（``--home``）には一切触れない（壊れていても
    # 答えられる）。
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="版と実行元のパッケージディレクトリを表示して終了する",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        help="資源の状態を表示する（常に全件）",
        description=(
            "宣言のある全資源を表示する。資源 ID は受け取らない——名指しで絞ると、"
            "表記の揺れ（大文字小文字は別資源）で相手の宣言が見えず「空き」と誤って"
            "答えることがあるため（issue #9）。"
        ),
    )
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
            return EXIT_BROKEN
        if command == "release":
            # **release は破壊的操作である。** ここで 0 を返すと、宣言を 1 件も
            # 消せていないのに「解放した」と読まれる——catch-all も「終了コードで
            # 嘘をつかない」（cli.py 冒頭）の対象である。フックと非破壊コマンド
            # （status / claim / update / history）の catch-all は引き続き
            # fail-open のまま 0 を返す（issue #17 指摘 5）。
            return EXIT_BROKEN
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
