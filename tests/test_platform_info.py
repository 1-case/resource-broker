"""OS 依存層の守護テスト。

ここで守るのは**起動時刻の逆算**である。幽霊判定で唯一「確定」とされている条件は
``since < 起動時刻`` の 1 つだけであり、その入力がこの層で作られる。
壊れると**稼働中の宣言が「再起動前の幽霊」として退去される**。倒れる向きが最悪なので、
「注意する」ではなくテストで固定する。

CI は Linux で回るため、Windows の実 API は呼べない。``tick_millis`` を注入して、
32 bit 相当の負値・巻き戻り・巨大値を与えたときの振る舞いを検証する。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from resource_broker import clock, platform_info

#: ``GetTickCount64`` が返しうる 64 bit の値の例（稼働 60 日相当）。
SIXTY_DAYS_MS = 60 * 24 * 3600 * 1000


def boot_with(value: object) -> object:
    """指定した tick を与えて起動時刻を求める。"""
    return platform_info.boot_time(tick_millis=lambda: value)


def test_a_64bit_tick_yields_a_plausible_boot_time() -> None:
    """正しい 64 bit の値からは、素直に起動時刻が求まる。

    ``ctypes`` の既定の戻り値は ``c_int``（32 bit 符号付き）なので、``restype`` を
    宣言しないとこの値は表現できない。稼働 24.9 日を超えた時点で崩れる。
    """
    boot = boot_with(SIXTY_DAYS_MS)

    assert boot is not None
    elapsed = clock.now() - boot
    assert timedelta(days=59) < elapsed < timedelta(days=61)


def test_a_negative_tick_is_refused() -> None:
    """負値は判定材料にしない。

    32 bit 符号付きとして解釈すると、稼働 24.9 日で ``GetTickCount64`` の戻り値が
    負になる。ここを通すと起動時刻が**未来**になり、生きている宣言が軒並み
    「起動より前の宣言」＝幽霊として退去される。
    """
    assert boot_with(-1) is None
    assert boot_with(-(2**31)) is None


def test_a_wrapped_tick_does_not_move_the_boot_time_forward() -> None:
    """下位 32 bit へ巻き戻った値でも、起動時刻が実際より新しくならない……とは限らない。

    巻き戻りは「小さな正の値」に化けるため、値だけを見て検出することはできない。
    **検出できないからこそ ``restype`` の宣言が唯一の防御である**。ここでは
    「巻き戻り相当の小さな値でも例外を出さず、時刻として矛盾しない値になる」ことだけを
    確かめ、防御の本体は :func:`test_a_64bit_tick_yields_a_plausible_boot_time` が守る。
    """
    boot = boot_with(SIXTY_DAYS_MS % (2**32))

    assert boot is not None
    assert boot <= clock.now()


def test_an_absurdly_large_tick_is_refused() -> None:
    """上限を超えた値は判定材料にしない。

    ``restype`` を取り違えたまま符号なしとして読むと、負値が巨大な正値に化ける。
    そのまま引くと起動時刻が数百年前になり、**全ての宣言が幽霊になる**。
    """
    assert boot_with(2**63) is None
    assert boot_with(float("inf")) is None


def test_a_broken_tick_value_is_refused() -> None:
    """数値でない値・NaN・ゼロは判定材料にしない。"""
    for value in (None, "60000", True, float("nan"), 0):
        assert boot_with(value) is None, f"{value!r} を通してしまった"


def test_a_failing_probe_does_not_raise() -> None:
    """取得そのものが失敗しても例外を出さない（判定材料が無いだけ）。

    この層の約束は「失敗時は None を返し、例外を投げない」である。ここから例外が出ると、
    掲示板を読む全ての経路が巻き込まれる。
    """

    def explode() -> object:
        raise OSError("API が呼べない")

    assert platform_info.boot_time(tick_millis=explode) is None


def test_uptime_sanity_check_keeps_ordinary_values() -> None:
    """正常な範囲の値はそのまま通す。sanity check が実運用を壊さないこと。"""
    assert platform_info._sane_uptime(1.5) == 1.5
    assert platform_info._sane_uptime(platform_info.MAX_UPTIME_S) is not None


# --- macOS の起動時刻（実機が無いので注入で検証する） -----------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{ sec = 1755300000, usec = 123456 } Sat Aug 16 09:00:00 2026", 1755300000),
        ("{ sec=1755300000, usec=1 }", 1755300000),
        ("{ sec = 1755300000 }", 1755300000),
    ],
)
def test_boottime_output_is_parsed(text: str, expected: int) -> None:
    """``sysctl -n kern.boottime`` の出力から epoch 秒を取り出す。"""
    assert platform_info.parse_boottime(text) == expected


@pytest.mark.parametrize("text", ["", "なにこれ", "{ usec = 1 }", None, 123, b"sec = 1"])
def test_unreadable_boottime_is_not_guessed(text: object) -> None:
    """読めない出力から値を作らない。**判定材料が無いことは None で表す。**"""
    assert platform_info.parse_boottime(text) is None


def test_darwin_boot_time_is_used_when_injected() -> None:
    """macOS 経路は起動時刻を直接使う（経過時間からの逆算をしない）。

    macOS には ``/proc/uptime`` が無い。塞がないと boot_time が None を返し続け、
    幽霊判定 3 根拠のうち**唯一「確定的」な ``since < 起動時刻`` が静かに無効化される**。
    """
    epoch = clock.now().timestamp() - 3600  # 1 時間前に起動した

    boot = platform_info.boot_time(boot_epoch=lambda: epoch)

    assert boot is not None
    assert boot.tzinfo is not None  # 掲示板の時刻は必ずオフセット付き
    assert abs((clock.now() - boot).total_seconds() - 3600) < 5


@pytest.mark.parametrize(
    "value",
    [
        None,  # sysctl が無い・失敗した
        "sec = 1755300000",  # 数値でない
        0,  # 非正
        -1,
        float("nan"),
        True,  # bool を数値として通さない
    ],
)
def test_a_broken_boot_epoch_yields_no_answer(value: object) -> None:
    """壊れた値から起動時刻を作らない。

    範囲外を通すと、**生きている宣言が「再起動前の幽霊」として退去されうる**。
    判定できないときは「退けない」側（None）へ倒す。
    """
    assert platform_info.boot_time(boot_epoch=lambda: value) is None


def test_a_future_boot_time_is_rejected() -> None:
    """起動時刻が未来にあるものを受け入れない（時計のずれ・改竄）。"""
    future = clock.now().timestamp() + 86400

    assert platform_info.boot_time(boot_epoch=lambda: future) is None


def test_an_ancient_boot_time_is_rejected() -> None:
    """10 年より前の起動時刻を受け入れない。"""
    ancient = clock.now().timestamp() - platform_info.MAX_UPTIME_S - 1

    assert platform_info.boot_time(boot_epoch=lambda: ancient) is None


def test_a_raising_boot_epoch_does_not_escape() -> None:
    """この層は例外を出さない（呼び出し側が fail-open で通せるように）。"""

    def explode() -> object:
        raise RuntimeError("sysctl が壊れた")

    assert platform_info.boot_time(boot_epoch=explode) is None
