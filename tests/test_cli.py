"""CLI の終了コードと出力を検証する。

終了コードの契約は「1 を返すのは、掲示板が正常に読めた上で使用中と判定できたときだけ」。
それ以外は 0 で通す（引数の不備だけは argparse の意図どおり 2 を返す）。

本ツールは資源を調べない。したがってここには**実機の資源に触れる経路が無い**。
GPU も COM ポートも、テストから見れば単なる文字列の ID である。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize


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

    assert row["occupied"] is True
    assert row["occupied"] is True
    assert row["declarations"][0]["holder"]["job"] == "実機の教示"
    assert row["declarations"][0]["log"] == "runs/probe.log"
    assert row["declarations"][0]["since"]


def test_status_reports_free_for_unclaimed_resource(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """宣言が無い資源は空きとして返る。"""
    assert run(tmp_path, "status", "COM7", "--json") == 0
    row = json.loads(capsys.readouterr().out)["resources"][0]

    assert row["occupied"] is False
    assert row["occupied"] is False


def test_claim_records_the_log_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """観測点（ログのパス）が掲示板に残る。

    掲示板は ETA を持たない。読む側がログを見て判断できるようにするためである。
    """
    claim(tmp_path, "COM3", "収録", "--log", "runs/rec.log")
    capsys.readouterr()
    run(tmp_path, "status", "COM3", "--json")

    row = json.loads(capsys.readouterr().out)["resources"][0]
    assert row["declarations"][0]["log"] == "runs/rec.log"


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

    observed = json.loads(capsys.readouterr().out)["resources"][0]["declarations"][0]["observed"]
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


def test_wait_returns_when_nothing_holds_the_resource(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """主宣言も相乗りも無ければ即座に戻る。"""
    assert run(tmp_path, "wait", "GPU0") == 0
    assert "既に解放されています" in capsys.readouterr().out


def test_an_interrupted_command_is_not_reported_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C を「使用中」と区別できる終了コードで返す。

    ``EXIT_BUSY`` は 1 なので、traceback で 1 が返ると呼び出し側から資源の競合と
    区別できない。シェルの慣習どおり 130 を返す。
    """

    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("resource_broker.cli._cmd_claim", interrupt)

    assert claim(tmp_path, "GPU0", "学習") == 130
    assert "中断しました" in capsys.readouterr().err


# --- 表示名は資源 ID を隠さない ---------------------------------------------------


def test_status_shows_the_resource_id_even_with_a_display_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--display`` にジョブ名を入れられても、一覧から資源 ID が消えない。

    実運用で display が ``malm E017 学習`` になり、GPU0 が押さえられていることが
    一覧から見えなくなった。取得の排他は資源 ID で効くので衝突そのものは
    起きないが、**掲示板は読まれて初めて意味を持つ**。GPU を使おうとする側は
    見覚えのないジョブ名しか見えず、空きだと誤読しうる状態だった。
    """
    claim(tmp_path, "GPU0", "E017 A/B 学習", "--display", "malm E017 学習")
    capsys.readouterr()

    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out

    assert "GPU0" in out
    assert "malm E017 学習" in out


def test_status_json_exposes_a_label_that_keeps_the_resource_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON でも見出し（label）に資源 ID が含まれる。"""
    claim(tmp_path, "GPU0", "E017 A/B 学習", "--display", "malm E017 学習")
    capsys.readouterr()

    assert run(tmp_path, "status", "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)["resources"]

    assert row["label"] == "GPU0（malm E017 学習）"
    # display そのものは申告された値のまま残す（意味を変えない）。
    assert row["display"] == "malm E017 学習"


def test_wait_names_the_resource_it_is_waiting_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """待機の表示にも資源 ID を出す。何を待っているか分からなくなる。"""
    claim(tmp_path, "GPU0", "E017 A/B 学習", "--display", "malm E017 学習")
    capsys.readouterr()

    assert run(tmp_path, "wait", "GPU0", "--timeout", "0") != 0
    out = capsys.readouterr().out

    assert "GPU0" in out


def test_status_never_calls_an_unreadable_board_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rb status`` が読めない掲示板を「空です」と断定しない。

    ``Path.glob`` は ``OSError`` を握り潰して空を返すため、掲示板が通常ファイルに
    なっているだけで「誰も資源を宣言していません」と出ていた。**実際には使われている
    資源を空きとして配る**——このツールが最もやってはならないことである。
    """
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    assert main(["--home", str(tmp_path), "status"]) == 0

    out = capsys.readouterr().out
    assert "掲示板は空です" not in out, out
    assert "空とは限りません" in out, out


def test_status_json_flags_an_unreadable_board(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` にも「読めなかった」を出す。機械が読む側にも同じ事実を渡す。"""
    (tmp_path / "board").write_text("これはディレクトリではない", encoding="utf-8")

    assert main(["--home", str(tmp_path), "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["partial"] is True
    assert payload["resources"] == []


def test_status_on_an_empty_board_is_not_flagged_as_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """本当に空なら「空です」と言う。**留保を付けすぎて意味を失わせない。**"""
    assert main(["--home", str(tmp_path), "status", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["partial"] is False


def test_share_lets_you_declare_alongside(tmp_path: Path) -> None:
    """``--share`` を付ければ、既に宣言のある資源へ並んで宣言できる。

    **claim と run の両方に配線されていること**まで確かめる。片方だけ繋がっていない
    状態でテストが全部緑だった——`acquire` 側の分岐だけ見ても、呼び出し側が値を
    渡していなければ意味が無い。
    """
    assert claim(tmp_path, "GPU0", "1 本目") == 0
    assert claim(tmp_path, "GPU0", "2 本目") == 1, "断らないなら段差の意味が無い"

    assert claim(tmp_path, "GPU0", "2 本目", "--share") == 0

    assert len(Board(tmp_path).list_for(normalize("GPU0"))) == 2


def test_share_is_wired_into_run(tmp_path: Path) -> None:
    """``rb run`` 側にも ``--share`` が繋がっている。"""
    assert claim(tmp_path, "GPU0", "1 本目") == 0

    code = main(
        [
            "--home",
            str(tmp_path),
            "run",
            "--res",
            "GPU0",
            "--job",
            "2 本目",
            "--observed",
            "見た",
            "--eta",
            "10m",
            "--share",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        ]
    )

    assert code == 0


def test_a_simultaneous_declaration_is_reported_to_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同じ瞬間に入った宣言は、**入れた側にその場で知らされる**。

    平坦化の代償として `O_EXCL` による「先着 1 名」が無くなった。読んでから書くまでの
    窓で 2 セッションが同時に通り抜けられる（窓を意図的に開くと 12 プロセス全員が通る）。
    条件付き書き込みのプリミティブが OS に無いので**窓は塞げない**。できるのは
    見えるようにすることで、それはこのツールの本来の仕事でもある。
    """
    import resource_broker.cli as cli_module

    real = cli_module.assess
    other = build_entry(normalize("GPU0"), job="ほぼ同時の相手", session="other")

    def racing(board, resource_id, observation=None):  # type: ignore[no-untyped-def]
        result = real(board, resource_id, observation)
        if not result:
            board.declare(other)  # 読み終えた直後に他セッションが入る
        return result

    monkeypatch.setattr(cli_module, "assess", racing)
    capsys.readouterr()

    assert claim(tmp_path, "GPU0", "私のジョブ") == 0

    err = capsys.readouterr().err
    assert "ほぼ同時" in err, err
    assert "ほぼ同時の相手" in err, err
