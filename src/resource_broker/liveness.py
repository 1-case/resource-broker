"""掲示板エントリが生きているかを判定する。純粋関数のみ。

実測の扱いは**非対称**である（CLAUDE.md「Liveness Judgment」）。

- 実測が「使用中」→ それだけで確定。誰が使っていようと、使えないことに変わりはない
- 実測が「空き」→ **宣言が幽霊である証明にはならない**。宣言はジョブが資源を掴む前に
  行われるし、長時間ジョブでも資源を使わない工程では空きに見える

したがって宣言を退けてよいのは次の 3 つだけである。

1. 再起動をまたいだ宣言（``since < boot``）。確定的
2. 猶予時間を過ぎ、かつ実測が空きで、かつ宣言プロセスが死んでいる
3. 明示的な解放または ``--force``（本モジュールの外の判断）

OS へのアクセスは一切行わない。材料をすべて引数で受け取ることで、
CI の Linux ARM64 上でも判定ロジックそのものを検証できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True)
class Observation:
    """セッションが資源を実際に調べた結果。

    **本ツールは資源を調べない。** 何をどう調べるかは資源ごとに異なり、それを実装に
    持てば「GPU なら nvidia-smi」のような資源固有の分岐が生まれる。有限資源一般を
    扱うという目的に反するし、調べ方は陳腐化する。調べるのは資源を使おうとする
    セッション（Claude Code）の仕事であり、ここにはその**結論だけ**が入る
    （DESIGN.md「Who Investigates」）。

    Attributes
    ----------
    busy : bool or None
        使用中なら True、空いていれば False、**調べていない・分からなければ None**。
        None は「情報が無い」であって「空いている」ではない。
    note : str
        何を見たかの生の記録。本ツールは**解釈しない**。掲示板に観測点として載せ、
        意味づけは読む側に任せる。
    """

    busy: bool | None = None
    note: str = ""

    @property
    def known(self) -> bool:
        """使用中か空きかの結論が出ているか。"""
        return self.busy is not None


#: 宣言してから実測に現れるまでの猶予。
#:
#: ``claim`` した直後はまだジョブが資源を掴んでいない（モデルのロードに数十秒から
#: 数分かかる）。この間に実測が「空き」を返すのは当然であり、それを幽霊と判定すると
#: 宣言した端から他セッションに奪われる。防ごうとしている衝突そのものになる。
DEFAULT_GRACE = timedelta(minutes=5)


class Verdict(StrEnum):
    """資源の状態についての判定。"""

    FREE = "free"
    """掲示板にエントリが無い。"""

    HELD = "held"
    """使用中。実測で確認できたか、有効な宣言がある。"""

    STALE_PROBE = "stale_probe"
    """猶予を過ぎ、実測が空きで、宣言プロセスも死んでいる。幽霊である。"""

    STALE_REBOOT = "stale_reboot"
    """宣言が再起動より前のもの。**確定的な幽霊**である。"""

    UNCERTAIN = "uncertain"
    """宣言はあるが裏が取れない。PID が死んでいる、時刻が壊れている等。"""


#: 資源を取得してよい判定。
FREE_VERDICTS = frozenset({Verdict.FREE, Verdict.STALE_PROBE, Verdict.STALE_REBOOT})


def judge(
    *,
    has_entry: bool,
    since: datetime | None,
    boot: datetime | None,
    observation: Observation,
    pid_alive: bool | None,
    now: datetime,
    grace: timedelta = DEFAULT_GRACE,
) -> Verdict:
    """資源の状態を判定する。

    Parameters
    ----------
    has_entry : bool
        掲示板にエントリが存在するか。
    since : datetime or None
        エントリの宣言時刻。解釈できなかった場合は None。
    boot : datetime or None
        マシンの起動時刻。取得できなかった場合は None。
    observation : Observation
        実測の結果。``busy`` が None なら判定材料にしない。
    pid_alive : bool or None
        宣言に書かれた PID の生存。判定できなければ None。
        手動の ``claim`` では PID を記録しないため、通常は None である。
    now : datetime
        現在時刻。猶予時間の判定に使う。呼び出し側が明示的に渡す。
    grace : timedelta
        宣言してから実測に現れるまでの猶予。

    Returns
    -------
    Verdict
        判定結果。

    Notes
    -----
    実測が「使用中」を示すときは、それだけで確定する。誰が使っていようと、
    使えないことに変わりはないためである。一方で実測が「空き」を示しても、
    宣言が幽霊である証明にはならない。宣言はジョブが資源を掴む前に行われ、
    長時間ジョブでも資源を使わない工程では空きに見えるためである。
    """
    if not has_entry:
        return Verdict.FREE

    # 1. 実測が使用中。誰が使っていようと使えない。
    if observation.busy is True:
        return Verdict.HELD

    # 2. 再起動をまたいだ宣言は確定的な幽霊。全 PID が無効になっている。
    if boot is not None and since is not None and since < boot:
        return Verdict.STALE_REBOOT

    # 3. 宣言時刻が壊れていると猶予を計算できない。裏が取れないとして扱う。
    if since is None:
        return Verdict.UNCERTAIN

    # 4. 未来の宣言時刻。時計のずれや手編集で起きる。そのまま扱うと `now - since < grace` が
    #    常に真になり、**永久に退かせない宣言**ができる。裏が取れないものとして扱う。
    if since > now + grace:
        return Verdict.UNCERTAIN

    # 5. 宣言直後はまだ資源を掴んでいなくて当然。猶予の間は宣言を信じる。
    if now - since < grace:
        return Verdict.HELD

    # 6. 猶予を過ぎ、実測が空きで、宣言プロセスも死んでいる。三点そろって初めて幽霊とする。
    if observation.busy is False and pid_alive is False:
        return Verdict.STALE_PROBE

    # 7. 宣言プロセスが死んでいるが、実測で裏が取れない。空きとはみなさない。
    if pid_alive is False:
        return Verdict.UNCERTAIN

    # 8. 宣言を信じる。
    return Verdict.HELD


def is_free(verdict: Verdict) -> bool:
    """その判定のときに資源を取得してよいか。

    ``UNCERTAIN`` は含めない。掲示板が正常に読めた上で裏が取れないだけなので、
    ここは fail-safe に倒す（インフラの故障と資源の競合を混同しない）。
    """
    return verdict in FREE_VERDICTS


def explain(verdict: Verdict) -> str:
    """判定の理由を日本語 1 行で返す。CLI とフックの説明文に使う。"""
    return {
        Verdict.FREE: "掲示板にエントリが無い",
        Verdict.HELD: "実測で使用を確認、または宣言が有効",
        Verdict.STALE_PROBE: "猶予を過ぎ、実測が空きで宣言プロセスも消えている（幽霊）",
        Verdict.STALE_REBOOT: "宣言が再起動より前のもの（確定的な幽霊）",
        Verdict.UNCERTAIN: "宣言はあるが裏が取れない（PID 消失または時刻不正）",
    }[verdict]
