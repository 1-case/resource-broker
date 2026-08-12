"""CLI の終了コードと出力を検証する。

終了コードの契約は「1 を返すのは、掲示板が正常に読めた上で使用中と判定できたときだけ」。
それ以外は 0 で通す（引数の不備だけは argparse の意図どおり 2 を返す）。

本ツールは資源を調べない。したがってここには**実機の資源に触れる経路が無い**。
GPU も COM ポートも、テストから見れば単なる文字列の ID である。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resource_broker.cli import main


def run(tmp_path: Path, *args: str) -> int:
    """一時的な掲示板に対して CLI を実行する。"""
    return main(["--home", str(tmp_path), *args])


def claim(tmp_path: Path, resource: str, job: str, *extra: str) -> int:
    """必須項目を埋めた claim。``--observed`` と ``--eta`` は省略できない。"""
    return run(
        tmp_path,
        "claim",
        resource,
        "--job",
        job,
        "--observed",
        "調べた",
        "--eta",
        "30m",
        *extra,
    )


def test_status_on_empty_board_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """誰も宣言していなければ 0 を返す。"""
    assert run(tmp_path, "status") == 0
    assert capsys.readouterr().out


def test_claim_requires_the_observation(tmp_path: Path) -> None:
    """``--observed`` なしでは宣言できない。

    本ツールの強制はここにある。「調べたか」を問わずに宣言させると、
    掲示板は「誰かが場所取りした」以上の意味を持たなくなる。
    """
    assert run(tmp_path, "claim", "COM3", "--job", "実機の教示") == 2


def test_claim_then_second_claim_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 本目は成功し、2 本目は使用中として 1 を返す。"""
    assert claim(tmp_path, "COM3", "実機の教示") == 0
    capsys.readouterr()

    assert claim(tmp_path, "COM3", "別の作業") == 1
    err = capsys.readouterr().err
    assert "使用中" in err
    assert "実機の教示" in err  # 誰が何をしているかが分かる


def test_self_reported_busy_blocks_the_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自分の調査で使用中だったなら、掲示板が空でも宣言しない。

    実測「使用中」は単独で確定する（CLAUDE.md「Liveness Judgment」）。
    未宣言の使用者がいる状況で場所取りだけしても衝突は防げない。
    """
    assert (
        run(
            tmp_path,
            "claim",
            "GPU0",
            "--job",
            "学習",
            "--observed",
            "nvidia-smi: compute apps 1 件",
            "--eta",
            "1h",
            "--found",
            "busy",
        )
        == 1
    )
    assert "使用中" in capsys.readouterr().err


def test_freshly_claimed_resource_is_not_stolen(tmp_path: Path) -> None:
    """宣言直後に「空き」と申告しても、他セッションに奪われない（回帰テスト）。

    実機のスモークテストで発覚した不具合。``claim`` の時点ではジョブがまだ資源を
    掴んでいないため、調べれば当然「空き」に見える。それを幽霊と判定していたため、
    宣言した端から次の claim が通っていた。
    """
    assert claim(tmp_path, "GPU0", "モデルのロード中", "--found", "free") == 0
    assert claim(tmp_path, "GPU0", "割り込み", "--found", "free") == 1


def test_force_overrides_a_live_declaration(tmp_path: Path) -> None:
    """--force は使用中でも取得する。"""
    claim(tmp_path, "COM3", "先客")

    assert claim(tmp_path, "COM3", "割り込み", "--force") == 0


def test_release_allows_reclaim(tmp_path: Path) -> None:
    """解放すれば次のセッションが取得できる。"""
    claim(tmp_path, "COM3", "1 本目")

    assert run(tmp_path, "release", "COM3") == 0
    assert claim(tmp_path, "COM3", "2 本目") == 0


def test_release_of_absent_declaration_succeeds(tmp_path: Path) -> None:
    """宣言が無い状態の解放はエラーにしない（冪等）。"""
    assert run(tmp_path, "release", "COM3") == 0


def test_status_json_exposes_verdict_and_holder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json は判定と宣言者を機械可読で返す（フックが読む形式）。"""
    claim(tmp_path, "COM3", "実機の教示", "--log", "runs/probe.log")
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
    claim(tmp_path, "COM3", "収録", "--log", "runs/rec.log")
    capsys.readouterr()
    run(tmp_path, "status", "COM3", "--json")

    row = json.loads(capsys.readouterr().out)["resources"][0]
    assert row["log"] == "runs/rec.log"


def test_claim_records_the_observation_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """申告された観測は、解釈されずそのまま掲示板に残る。

    本ツールは観測の中身を読まない。資源ごとに書式が違うためであり、
    解釈は掲示板を読む側（次のセッション）に任せる。
    """
    run(
        tmp_path,
        "claim",
        "\\\\nas\\share",
        "--job",
        "バックアップ",
        "--observed",
        "net use: 接続なし / 空き容量 2.1TB",
        "--eta",
        "2h",
        "--found",
        "free",
    )
    capsys.readouterr()
    run(tmp_path, "status", "\\\\nas\\share", "--json")

    observed = json.loads(capsys.readouterr().out)["resources"][0]["observed"]
    assert observed["note"] == "net use: 接続なし / 空き容量 2.1TB"
    assert observed["found"] == "free"
    assert observed["at"]  # 観測時刻は機械が刻む


def test_status_without_arguments_lists_only_declared_resources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """引数なしの status は、宣言のある資源だけを出す。

    「このマシンにある資源」を本ツールは知らない。列挙しようとすれば資源種別を
    実装に持つことになる。掲示板に載っているものだけが本ツールの知り得る全てである。
    """
    claim(tmp_path, "COM3", "実機の教示")
    capsys.readouterr()

    assert run(tmp_path, "status", "--json") == 0
    resources = json.loads(capsys.readouterr().out)["resources"]

    assert [row["display"] for row in resources] == ["COM3"]
