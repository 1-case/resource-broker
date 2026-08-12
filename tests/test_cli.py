"""CLI の終了コードと出力を検証する。

終了コードの契約は「1 を返すのは、掲示板が正常に読めた上で使用中と判定できたときだけ」。
それ以外は 0 で通す。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resource_broker.cli import main
from resource_broker.probes.base import FakeProbe, Observation


@pytest.fixture(autouse=True)
def _no_real_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """実機のプローブを呼ばない。

    CI には GPU が無く、開発機では実 GPU を占有してはならない。
    """
    monkeypatch.setattr("resource_broker.cli.probe_for", lambda _r: None)


def run(tmp_path: Path, *args: str) -> int:
    """一時的な掲示板に対して CLI を実行する。"""
    return main(["--home", str(tmp_path), *args])


def test_status_on_empty_board_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """誰も宣言していなければ 0 を返す。"""
    assert run(tmp_path, "status") == 0
    assert capsys.readouterr().out


def test_claim_then_second_claim_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 本目は成功し、2 本目は使用中として 1 を返す。"""
    assert run(tmp_path, "claim", "COM3", "--job", "実機の教示") == 0
    capsys.readouterr()

    assert run(tmp_path, "claim", "COM3", "--job", "別の作業") == 1
    err = capsys.readouterr().err
    assert "使用中" in err
    assert "実機の教示" in err  # 誰が何をしているかが分かる


def test_freshly_claimed_resource_is_not_stolen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宣言直後に実測が空きを返しても、他セッションに奪われない（回帰テスト）。

    実機のスモークテストで発覚した不具合。``claim`` の時点ではジョブがまだ資源を
    掴んでいないため実測は当然「空き」を返す。それを幽霊と判定していたため、
    宣言した端から次の claim が通っていた。
    """
    monkeypatch.setattr(
        "resource_broker.cli.probe_for", lambda _r: FakeProbe(Observation(busy=False))
    )

    assert run(tmp_path, "claim", "GPU0", "--job", "モデルのロード中") == 0
    assert run(tmp_path, "claim", "GPU0", "--job", "割り込み") == 1


def test_force_overrides_a_live_declaration(tmp_path: Path) -> None:
    """--force は使用中でも取得する。"""
    run(tmp_path, "claim", "COM3", "--job", "先客")

    assert run(tmp_path, "claim", "COM3", "--job", "割り込み", "--force") == 0


def test_release_allows_reclaim(tmp_path: Path) -> None:
    """解放すれば次のセッションが取得できる。"""
    run(tmp_path, "claim", "COM3", "--job", "1 本目")

    assert run(tmp_path, "release", "COM3") == 0
    assert run(tmp_path, "claim", "COM3", "--job", "2 本目") == 0


def test_release_of_absent_declaration_succeeds(tmp_path: Path) -> None:
    """宣言が無い状態の解放はエラーにしない（冪等）。"""
    assert run(tmp_path, "release", "COM3") == 0


def test_status_json_exposes_verdict_and_holder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json は判定と宣言者を機械可読で返す（フックが読む形式）。"""
    run(tmp_path, "claim", "COM3", "--job", "実機の教示", "--log", "runs/probe.log")
    capsys.readouterr()

    assert run(tmp_path, "status", "COM3", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["resources"][0]

    assert row["free"] is False
    assert row["verdict"] == "held"
    assert row["holder"]["job"] == "実機の教示"
    assert row["log"] == "runs/probe.log"
    assert row["since"]


def test_status_reports_free_for_unclaimed_resource(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """宣言が無い資源は空きとして返る。"""
    assert run(tmp_path, "status", "COM7", "--json") == 0
    row = json.loads(capsys.readouterr().out)["resources"][0]

    assert row["free"] is True
    assert row["verdict"] == "free"


def test_claim_records_the_log_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """観測点（ログのパス）が掲示板に残る。

    掲示板は ETA を持たない。読む側がログを見て判断できるようにするためである。
    """
    run(tmp_path, "claim", "COM3", "--job", "収録", "--log", "runs/rec.log")
    capsys.readouterr()
    run(tmp_path, "status", "COM3", "--json")

    row = json.loads(capsys.readouterr().out)["resources"][0]
    assert row["log"] == "runs/rec.log"
