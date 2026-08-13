"""ETA・利用見積もり・相乗り可否の申告を検証する。

これらは**セッションが申告する値**であり、本ツールは解釈しない。`--observed` と同じ枠である。
唯一の例外が ETA の絶対時刻で、そこは**機械が計算する**（LLM に足し算をさせない）。

ETA を必須にしているのは、正確な値が欲しいからではなく「どれくらいで終わるか」を
一度考えさせるためである。外れても本ツールは何も判断しない。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from resource_broker import clock
from resource_broker.cli import main


def run(tmp_path: Path, *args: str) -> int:
    return main(["--home", str(tmp_path), *args])


def claim(tmp_path: Path, *extra: str, eta: str = "30m") -> int:
    return run(
        tmp_path,
        "claim",
        "GPU0",
        "--job",
        "E059 eval",
        "--observed",
        "nvidia-smi: compute apps なし",
        "--eta",
        eta,
        *extra,
    )


def row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    run(tmp_path, "status", "GPU0", "--json")
    return json.loads(capsys.readouterr().out)["resources"][0]


# --- ETA -------------------------------------------------------------------------


def test_eta_is_required(tmp_path: Path) -> None:
    """ETA なしでは宣言できない。

    ``--observed`` と並ぶ 2 つ目の強制である。書かせること自体に意味がある。
    """
    code = run(tmp_path, "claim", "GPU0", "--job", "x", "--observed", "調べた")

    assert code == 2


def test_duration_is_converted_by_the_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """期間表記は機械が絶対時刻に直す。

    「30 分後は何時か」を LLM に書かせない。JST と UTC の取り違えや単純な足し算の
    誤りが実際に起きている（CLAUDE.md「Time Handling」）。
    """
    before = clock.now()
    claim(tmp_path, eta="1h30m")
    capsys.readouterr()

    eta = row(tmp_path, capsys)["eta"]
    assert eta["stated"] == "1h30m"

    at = clock.parse_iso(eta["at"])
    assert at is not None
    delta = at - before
    assert timedelta(minutes=89) <= delta <= timedelta(minutes=91)


def test_free_text_eta_is_kept_without_a_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """解釈できない ETA も受け付ける。申告文だけを残す。

    「モデル次第」のような答えを禁じると、嘘の数字を書かせることになる。
    """
    claim(tmp_path, eta="モデルのロード次第")
    capsys.readouterr()

    eta = row(tmp_path, capsys)["eta"]
    assert eta["stated"] == "モデルのロード次第"
    assert eta["at"] is None


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("30m", 1800), ("2h", 7200), ("1d", 86400), ("1h30m", 5400)],
)
def test_duration_formats(text: str, seconds: int) -> None:
    """期間表記の解釈。"""
    assert clock.parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", None, "そのうち", "0m", "-5m", "abc"])
def test_unparsable_durations_return_none(text: str | None) -> None:
    """解釈できない期間は None（例外にしない）。"""
    assert clock.parse_duration(text) is None


# --- 利用見積もりと相乗り --------------------------------------------------------


def test_usage_and_sharing_are_kept_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """見積もりと相乗り可否は、解釈されずそのまま残る。

    単位も尺度も資源ごとに違う（VRAM の GB、CPU のコア数、API のリクエスト毎分）。
    本ツールが解釈すれば、その資源を知ることになる。
    """
    claim(
        tmp_path,
        "--peak",
        "VRAM 6GB / 瞬時最大 80%",
        "--avg",
        "VRAM 5GB / 平均 40%",
        "--sharing",
        "可（VRAM 残 2GB まで。要連絡）",
    )
    capsys.readouterr()

    data = row(tmp_path, capsys)
    assert data["usage"]["peak"] == "VRAM 6GB / 瞬時最大 80%"
    assert data["usage"]["avg"] == "VRAM 5GB / 平均 40%"
    assert data["sharing"] == "可（VRAM 残 2GB まで。要連絡）"


def test_usage_is_optional(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """見積もりは任意。書けないときに嘘を書かせない。"""
    claim(tmp_path)
    capsys.readouterr()

    data = row(tmp_path, capsys)
    assert data["usage"] is None
    assert data["sharing"] is None


def test_sharing_does_not_change_exclusivity(tmp_path: Path) -> None:
    """相乗り可と書いても、掲示板は排他のままである。

    可否を決めるのは当事者であって本ツールではない。旗を運ぶだけで、
    2 人目の宣言を通したりはしない。
    """
    claim(tmp_path, "--sharing", "可")

    second = run(
        tmp_path,
        "claim",
        "GPU0",
        "--job",
        "相乗りのつもり",
        "--observed",
        "掲示板に相乗り可とある",
        "--eta",
        "10m",
    )

    assert second == 1


# --- 履歴 -----------------------------------------------------------------------


def test_history_shows_past_estimates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """過去の申告を振り返れる。次の見積もりの根拠になる。

    見積もりの精度は 1 回目には期待できない。2 回目・3 回目で上げていくには、
    前回どう見積もったかを見返せる必要がある。
    """
    claim(tmp_path, "--peak", "VRAM 6GB", eta="40m")
    run(tmp_path, "release", "GPU0")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0") == 0
    out = capsys.readouterr().out

    assert "E059 eval" in out
    assert "40m" in out
    assert "VRAM 6GB" in out


def test_history_is_empty_without_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """履歴が無くてもエラーにしない。"""
    assert run(tmp_path, "history") == 0
    assert "見つかりません" in capsys.readouterr().out


# --- 実績（宣言と解放の時刻差）の突き合わせ ---------------------------------------


def test_history_shows_the_actual_elapsed_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """申告 ETA の横に**実所要**が出る。

    申告だけを並べても「突き合わせろ」と言えるだけで材料が無い。実所要は
    宣言と解放の時刻差から出す（どちらも機械生成なので、LLM の記憶に頼らない）。
    """
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    claim(tmp_path, eta="40m")

    # 10 分で終わった、という時間の進み方を作る。
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=10))
    run(tmp_path, "release", "GPU0")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0") == 0
    out = capsys.readouterr().out

    assert "40m" in out  # 申告
    assert "10m" in out  # 実績
    assert "0.25 倍" in out  # 申告に対する比


def test_history_does_not_aggregate_across_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """案件をまたいだ集計値（中央値・平均）を出さない。

    案件ごとに規模も予測しやすさも違うので、均した値は意味を持たない。しかも
    集計値は「次は 1/4 で申告すればいい」という機械的な補正を誘い、ETA を必須に
    した目的（一度考えさせる）と正反対に働く。突き合わせるのは同じ案件の前回である。
    """
    base = clock.now()
    for index in range(3):
        start = base + timedelta(hours=index)
        monkeypatch.setattr(clock, "now", lambda start=start: start)
        claim(tmp_path, eta="40m")
        monkeypatch.setattr(clock, "now", lambda start=start: start + timedelta(minutes=20))
        run(tmp_path, "release", "GPU0")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0") == 0
    out = capsys.readouterr().out

    # 案件ごとの比は出る（自分の申告と自分の実績の対比なので意味がある）。
    assert out.count("0.50 倍") == 3
    # 全体を均した値は出さない。
    assert "中央値" not in out
    assert "平均" not in out
    assert "3 件" not in out


def test_history_does_not_invent_an_elapsed_time_for_a_live_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """まだ解放されていない宣言に実績を作らない。

    「解放が記録されていない」を「0 分で終わった」と混同すると、実績が
    実際より短い方向へ歪み、次の申告を短くしてしまう。
    """
    claim(tmp_path, eta="40m")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0") == 0
    out = capsys.readouterr().out

    assert "解放の記録なし" in out
    assert "倍" not in out


def test_history_json_carries_the_actual_and_stated_seconds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON でも実績を機械可読な形で返す。"""
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    claim(tmp_path, eta="40m")
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=10))
    run(tmp_path, "release", "GPU0")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    (record,) = payload["claims"]
    assert record["elapsed_seconds"] == 600
    assert record["stated_seconds"] == 2400
    assert record["release_reason"] == "release コマンド"


def test_history_does_not_reuse_one_release_for_two_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 件の解放を 2 つの宣言に割り当てない。

    取り違えると、古い宣言に新しい解放が付いて実績が実際より長く出る。
    """
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    claim(tmp_path, eta="40m")
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=10))
    run(tmp_path, "release", "GPU0")
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=30))
    claim(tmp_path, eta="40m")
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    first, second = payload["claims"]
    assert first["elapsed_seconds"] == 600
    assert second["elapsed_seconds"] is None
