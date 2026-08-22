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
from resource_broker.board import Board, build_entry
from resource_broker.cli import main
from resource_broker.naming import normalize


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
    """その資源の**最初の宣言**を機械可読な形で取り出す。

    平坦化で資源ごとの行は ``declarations`` の配列を持つようになった。1 件だけを
    見たいテストのための入り口である。
    """
    run(tmp_path, "status", "GPU0", "--json")
    resource = json.loads(capsys.readouterr().out)["resources"][0]
    declarations = resource["declarations"]
    return {**resource, **(declarations[0] if declarations else {})}


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


# --- nonce による対応付け（issue #15 #6） -----------------------------------------


def test_history_pairs_by_nonce_when_two_declarations_share_resource_and_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同じ資源・同じ job の宣言が並んでいても、``--nonce`` で消した方に解放が付く。

    以前の監査ログは nonce を持たず、``rb history`` は (資源, job) と時刻順だけで
    対応付けていた。同じ資源・同じ job の宣言 A・B が並び ``--nonce`` で B だけを
    消すと、時刻順で先に来た A（まだ生きている方）に解放が誤って割り当たっていた
    ——「消していない A を消したことにし、消した B には解放の記録が無い」という
    嘘を履歴が語ることになる。
    """
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    claim(tmp_path)  # A: since = base
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=1))
    claim(tmp_path, "--share")  # B: since = base + 1m。A と同じ資源・同じ job

    board = Board(tmp_path)
    a, b = board.list_for(normalize("GPU0"))  # since 昇順 = 宣言順
    assert a.job == b.job == "E059 eval"

    # B だけを --nonce で消す。A は生きたまま残る。
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=10))
    assert run(tmp_path, "release", "--nonce", b.nonce[:8]) == 0
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    by_nonce = {c["nonce"]: c for c in payload["claims"]}
    assert by_nonce[a.nonce]["released_at"] is None, "まだ生きている A に解放が付いた"
    assert by_nonce[b.nonce]["elapsed_seconds"] == 540, "消した B に解放が対応付かない"
    # **確実な対応付け（両方が nonce を持つ）は不確実フラグを立てない。**
    assert by_nonce[a.nonce]["pairing_uncertain"] is False
    assert by_nonce[b.nonce]["pairing_uncertain"] is False


def test_history_falls_back_to_job_pairing_for_audit_logs_without_nonce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """nonce を持たない古い監査ログでも ``rb history`` は壊れない。

    この対応付けより前に書かれたログには ``nonce`` フィールドが無い。対応付けの鍵を
    nonce 必須にすると、過去のログが一切読めなくなる。資源 + job へフォールバック
    することを、nonce を書かずに直接監査ログへ追記して確かめる。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    board.audit("claimed", resource=normalize("GPU0"), job="旧形式のジョブ", eta={"stated": "40m"})
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=5))
    board.audit(
        "removed", resource=normalize("GPU0"), job="旧形式のジョブ", reason="release コマンド"
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    (record,) = payload["claims"]
    assert record.get("nonce") is None
    assert record["elapsed_seconds"] == 300
    # **どちらも nonce を持たないフォールバック対応付けは不確実だと表明する。**
    assert record["pairing_uncertain"] is True


# --- 新旧の監査ログが混在するローリング更新（issue #17 指摘 5） --------------------


def test_history_pairs_an_old_claim_without_nonce_to_a_new_removal_with_nonce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**古い ``claimed``（nonce 無し）と新しい ``removed``（nonce 有り）が混在しても対応が付く。**

    ローリング更新で普通に起きる組み合わせである——宣言した時点では旧バージョンの
    コードが動いていて ``claimed`` 監査に ``nonce`` を書かなかったが、解放した時点
    では新バージョンに更新済みで ``removed`` 監査には ``nonce`` が書かれる。単純に
    「両方が nonce を持てば nonce で決め打つ」だけだと、鍵の種類がそもそも揃わない
    （旧側は資源+job、新側は資源+nonce）ため**絶対に一致しない**。2 段階の対応付け
    （nonce 同士を先に、残りを資源+job のフォールバックへ）で拾えることを固定する。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    # 宣言時は旧バージョン: claimed に nonce が無い。
    board.audit("claimed", resource=normalize("GPU0"), job="混在ケース", eta={"stated": "40m"})
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=7))
    # 解放時は新バージョンに更新済み: removed には nonce がある。
    board.audit(
        "removed",
        resource=normalize("GPU0"),
        job="混在ケース",
        nonce="a" * 32,
        reason="release コマンド",
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    (record,) = payload["claims"]
    assert record["elapsed_seconds"] == 420, "新旧混在で対応が付いていない"
    assert record["pairing_uncertain"] is True


def test_history_pairs_a_new_claim_with_nonce_to_an_old_removal_without_nonce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**逆方向（新しい ``claimed`` と古い ``removed``）でも対応が付く。**

    ローリング更新の途中で解放だけ旧バージョンのプロセスが行った場合を想定する。
    片方向だけを直すと、混在の半分しか救えない（issue #17 指摘 5 は「両方向」を
    明示している）。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    # 宣言時は新バージョン: claimed に nonce がある。
    board.audit(
        "claimed",
        resource=normalize("GPU0"),
        job="逆方向の混在",
        nonce="b" * 32,
        eta={"stated": "40m"},
    )
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=3))
    # 解放時は旧バージョン: removed に nonce が無い。
    board.audit(
        "removed", resource=normalize("GPU0"), job="逆方向の混在", reason="release コマンド"
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    (record,) = payload["claims"]
    assert record["elapsed_seconds"] == 180, "逆方向の新旧混在で対応が付いていない"
    assert record["pairing_uncertain"] is True


def test_history_does_not_let_a_mismatched_nonce_steal_the_fallback_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """nonce 付きの解放が**別の** nonce 付き宣言のものなら、無関係な宣言を奪わない。

    2 本の宣言（A: nonce 無し、B: nonce 有り）が並び、B に対応する nonce 付きの
    解放だけが記録されているとき、A はフォールバック（資源+job）で**別の**解放
    （こちらも記録されていれば）とだけ対応し、B 用の解放を横取りしてはならない。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    board.audit("claimed", resource=normalize("GPU0"), job="A・旧形式", eta={"stated": "40m"})
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=1))
    board.audit(
        "claimed",
        resource=normalize("GPU0"),
        job="A・旧形式",  # 同じ job にして、鍵が衝突しうる状況を作る
        nonce="c" * 32,
        eta={"stated": "40m"},
    )
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=5))
    # B（nonce 有り）だけを厳密に解放する。
    board.audit(
        "removed",
        resource=normalize("GPU0"),
        job="A・旧形式",
        nonce="c" * 32,
        reason="release --nonce コマンド",
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    a_record, b_record = payload["claims"]
    assert b_record.get("nonce") == "c" * 32
    assert b_record["released_at"] is not None
    assert b_record["pairing_uncertain"] is False, "厳密一致のはずが不確実になっている"
    # A は nonce を持たないので、B 用の nonce 一致解放を奪えない。
    assert a_record["released_at"] is None, "nonce の無い A が B の解放を横取りした"


# --- フォールバックは「少なくとも片側が nonce を欠く」組だけを対象にする（issue #18 指摘 6） -


def test_history_does_not_pair_two_records_that_both_carry_different_nonces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**両側が nonce を持ちながら値が違えば、それは「不確実」ではなく別個体だと確定している。**

    nonce は宣言ごとに一意なので、値の違う 2 つの記録が同じ宣言を指すことは
    あり得ない。以前のフォールバック（資源 + job + 時刻の近さ）は、この 2 つを
    区別せず「不確実な対応」として結び付けてしまっていた——無関係な 2 つの
    記録を誤って対応付ける（issue #18 指摘 6）。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    # A: 別の（記録されていない）解放を待っている宣言。nonce を持つ。
    board.audit(
        "claimed", resource=normalize("GPU0"), job="別個体", nonce="d" * 32, eta={"stated": "40m"}
    )
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=2))
    # X: A とは無関係な、別の宣言（ここでは記録されていない）の解放。nonce を持つが
    # A の nonce とは異なる。資源・job・時刻は「近い」ので、フォールバックが
    # 時刻だけで対応付けると誤って A に結び付いてしまう。
    board.audit(
        "removed",
        resource=normalize("GPU0"),
        job="別個体",
        nonce="f" * 32,
        reason="release コマンド",
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    (record,) = payload["claims"]
    assert record["nonce"] == "d" * 32
    # **対応付けてはならない。** 両側が nonce を持ち、値が違う以上、別個体だと
    # 確定している——「不確実」として結び付けるのは、以前の欠陥そのものである。
    assert record["released_at"] is None, "別個体の解放を誤って対応付けた"
    assert record["pairing_uncertain"] is False, "対応付けていないので不確実さを論じる対象が無い"


def test_history_deduplicates_removals_that_share_a_nonce_in_the_fallback_pool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**nonce 付きの ``removed`` はフォールバックの候補としても nonce 単位で重複排除する。**

    監査ログの追記は原子的でないため、同じ解放イベントが重複して書かれることが
    ある（再送・部分書き込みの再試行）。重複排除しないと、同じ nonce の解放が
    2 件とも資源 + job のフォールバック候補に残り、**2 つの別々の（nonce を
    持たない）宣言それぞれに「対応した」ことにされてしまう**——実際には
    1 件の解放しか起きていないのに、2 件とも解放済みだと嘘をつくことになる。
    """
    board = Board(tmp_path)
    base = clock.now()
    monkeypatch.setattr(clock, "now", lambda: base)
    # A・B: どちらも nonce を持たない旧形式の宣言。同じ資源・同じ job。
    board.audit("claimed", resource=normalize("GPU0"), job="重複解放", eta={"stated": "40m"})
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=1))
    board.audit("claimed", resource=normalize("GPU0"), job="重複解放", eta={"stated": "40m"})
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=2))
    # 同じ nonce を持つ解放が重複して記録される（監査ログの再送を模す）。
    # 実際に消えた宣言は 1 件だけである。
    board.audit(
        "removed", resource=normalize("GPU0"), job="重複解放", nonce="h" * 32, reason="release"
    )
    monkeypatch.setattr(clock, "now", lambda: base + timedelta(minutes=3))
    board.audit(
        "removed", resource=normalize("GPU0"), job="重複解放", nonce="h" * 32, reason="release"
    )
    capsys.readouterr()

    assert run(tmp_path, "history", "GPU0", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    released_count = sum(1 for c in payload["claims"] if c["released_at"] is not None)
    # **重複排除していれば、対応が付くのは高々 1 件。** 排除しなければ 2 件とも
    # 「解放済み」になり、実際には 1 回しか起きていない解放が 2 回起きたことになる。
    assert released_count <= 1, (
        "重複した removed が別々の宣言に対応付いた（重複排除が効いていない）"
    )


# --- はじいたときに次の一手を示す -------------------------------------------------


def hold(tmp_path: Path, *, sharing: str = "") -> None:
    """他セッションの宣言を 1 件置く（cwd を自分の外にして所有を切る）。"""
    board = Board(tmp_path)
    entry = build_entry(
        normalize("GPU0"),
        job="E061 スモーク",
        session="folnet",
        cwd=str(tmp_path / "other-session"),
        sharing=sharing,
    )
    assert board.declare(entry)


def test_refusal_shows_the_holders_sharing_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """はじくときに保持者の相乗り申告をそのまま見せる。

    実運用で、保持者が「可（VRAM 残 5GB まで）」と書いた GPU に対し、malm が
    claim ではじかれ、**相乗りできると気づかずに CPU へ逃げた**。掲示板は
    その材料を持っていたのに、判断が必要なその場で出していなかった。
    載せていても出さなければ、載せていないのと同じである。
    """
    hold(tmp_path, sharing="可（VRAM 残 5GB まで）")
    capsys.readouterr()

    assert claim(tmp_path) == 1
    err = capsys.readouterr().err

    assert "可（VRAM 残 5GB まで）" in err
    assert "--share" in err
    assert "rb wait" in err


def test_refusal_does_not_interpret_the_sharing_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """相乗り不可でも案内は同じ形で出す。**旗の中身で分岐しない。**

    可否は当事者が決めるものであり、本ツールは旗を運ぶだけである
    （CLAUDE.md「Resource Agnosticism」）。中身を読んで案内を出し分けると、
    そこから「ツールが可否を判断する」への距離が一気に縮む。
    """
    hold(tmp_path, sharing="不可（VRAM を使い切る）")
    capsys.readouterr()

    assert claim(tmp_path) == 1
    err = capsys.readouterr().err

    assert "不可（VRAM を使い切る）" in err
    assert "--share" in err


def test_refusal_is_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """はじいたことを監査ログに残す。

    残さないと「誰がいつ諦めたか」を後から追えない。判定したのに黙るのは、
    監視が死んだのと区別が付かない（CLAUDE.md「Silence Is Not Success」）。
    """
    hold(tmp_path, sharing="可")

    assert claim(tmp_path) == 1

    events = [
        json.loads(line)
        for path in sorted((tmp_path / "audit").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    (refused,) = [e for e in events if e.get("event") == "claim_refused"]
    assert refused["holder"] == "folnet"
    assert refused["sharing"] == "可"


def test_refusal_without_a_sharing_flag_still_points_somewhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """相乗りの申告が無くても、次の一手は示す。"""
    hold(tmp_path)
    capsys.readouterr()

    assert claim(tmp_path) == 1
    err = capsys.readouterr().err

    assert "相乗り:" not in err  # 申告が無いものを勝手に作らない
    assert "rb wait" in err


# --- 相乗りも振り返りの対象にする -------------------------------------------------


# --- 相乗りの合計超過を隠さない ---------------------------------------------------


def test_a_declared_timestamp_cannot_override_the_machine_one() -> None:
    """申告に ``at`` を書いても、掲示板に載るのは**機械が刻んだ時刻**である。

    「時刻はすべて機械生成」（DESIGN.md「Time Handling」）は、dict を組み立てる
    引数の順序 1 つで破れる。LLM が書いた時刻がそのまま載る経路を塞ぐ。
    """
    entry = build_entry(
        normalize("GPU0"),
        job="E059 eval",
        observed={"note": "nvidia-smi", "at": "2000-01-01T00:00:00+09:00"},
    )

    assert entry.observed is not None
    assert entry.observed["at"] != "2000-01-01T00:00:00+09:00"
    assert entry.observed["note"] == "nvidia-smi"
