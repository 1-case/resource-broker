"""``docs/DESIGN.md`` が「最終到達状態だけ」に留まっていることを機械で守る。

文書が膨らむのは、たいてい**経緯が混ざる**からである。1 つの判断について「何を決めたか」は
数行で済むが、「どう間違えて、どう気づいたか」は物語になる。両方を同じ文書に置くと、
読む側は仕様を探すのに物語を読まされ、書く側は改訂のたびにどちらを直すか迷う。

線引きは 1 つ。**「この文は将来も真か」**。真であり続けるなら仕様か根拠なので DESIGN へ、
過去形でしか書けないなら経緯なので ``EXPERIMENTS.md``（非公開の作業ログ）へ。

「注意する」だけでは守れないので、上限と混入をここで固定する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "DESIGN.md"

#: 行数の上限。増えたぶんはたいてい経緯である。
#: DESIGN.md の行数上限。
#:
#: 1175 行あった文書を「最終到達状態だけ」に絞ったときの実測（599 行）に合わせて置いた。
#: **上げるのは仕様が増えたときだけで、散文が増えたときではない。** 散文を止めるのは
#: この数ではなく :data:`NARRATIVE` と「私的文書へリンクしない」の 2 つである。
#:
#: 620 へ上げたのは、公開 API（``RESOURCE_BROKER_DISABLE``）と保持期限、そして
#: 受け入れた残余 3 件を書く場所が無くなったためである。**必要な仕様を番人が
#: 塞ぐなら、番人のほうが間違っている。**
#:
#: 621 へ上げたのは、``EXIT_BROKEN``（値 3）を Exit Codes の表へ 1 行足すため
#: である（issue #15 #5 の修正で追加した終了コード。表の抜けを埋める仕様の追加
#: であり、散文ではない）。
#:
#: 640 へ上げたのは、破壊的操作の族全体に「候補集合は完全か」を通した修正
#: （issue #17）で、Exit Codes に**コマンド × 結果**の対応表を足し、
#: Corrupt Entries と Waiting に完全性を見る／見ない経路の仕様を 1 段落ずつ
#: 足し、History の対応付けの鍵を 2 段階（nonce 優先・job フォールバック）へ
#: 更新したためである。いずれも「今後も真であり続ける仕様」であって経緯では
#: ない。
#:
#: 680 へ上げたのは、issue #18（型で強制する削除）の修正で、削除の公開入口が
#: ``ConfirmedEntry`` を要求する設計を Conditional Writes に 1 段落足し、
#: `OwnRemoval` / `ForcedRemoval` の `unconfirmed` を Releasing の表へ、
#: `--clean` の完全性判定を Corrupt Entries へ、幽霊退去の完全性ゲートを
#: Ghost Detection へ、`rb run` 後始末の再列挙しない仕様を Wrapper Spec へ、
#: `wait` の「その時点のポーリング」判定を Waiting へ、監査対応付けの
#: nonce 不一致除外を History へ、それぞれ 1 段落ずつ足したためである。
#: いずれも「今後も真であり続ける仕様」であって経緯ではない。
#:
#: 678 へ下げたのは、issue #9 の指摘を受けて 3 つの機能を削除したためである
#: （`rb status` の資源 ID 引数、`--display` と表示名の併記、`_remove_matching`
#: と旧い置き場 ``board/joins/`` の走査）。Resource Naming の表から表示名の列を、
#: Board Schema の例から ``display`` フィールドを、Declaration Fields から
#: `--display` を、それぞれ落とした。代わりに、nonce を持たない宣言をどう
#: 扱うか（``rb status`` には出すが、消せるのは ``--force`` と ``--clean``
#: だけ）を Conditional Writes に 1 段落足した——これも「今後も真であり
#: 続ける仕様」であって経緯ではない。**上げっぱなしにしない**：機能を消せば
#: 文書も軽くなるはずなので、実測に合わせて下げる。
MAX_LINES = 678

#: 公開しない文書。**公開物からここへリンクすると参照切れになる。**
PRIVATE_DOCS = ("EXPERIMENTS.md", "STATUS.md", "tools/speak.py", "tools/speak_dict.json")

#: 経緯の徴候。日付そのものは JSON の例示で正当に現れるので、**散文の中の日付**だけを見る。
NARRATIVE = re.compile(
    r"^(?!\s*[\"|#`}])(?=.*[ぁ-んァ-ヶ一-龥])"  # 日本語の散文行（コード・表・JSON を除く）
    r".*(?:\d{4}-\d{2}-\d{2}\s*(?:に|の|時点)|周にわたり|周続いた|回にわたって|"
    r"実測: |だった。|していた。|であった。)"
)


def lines() -> list[str]:
    return DESIGN.read_text(encoding="utf-8").splitlines()


def test_design_stays_within_its_budget() -> None:
    """行数の上限を超えない。**超えたぶんはたいてい経緯である。**"""
    count = len(lines())

    assert count <= MAX_LINES, (
        f"docs/DESIGN.md が {count} 行ある（上限 {MAX_LINES}）。"
        "経緯が混ざっていないか確かめること。経緯は EXPERIMENTS.md が正本である"
    )


@pytest.mark.parametrize("name", PRIVATE_DOCS)
def test_design_does_not_point_at_unpublished_files(name: str) -> None:
    """公開しない文書へリンクしない。**読者が辿れない参照は嘘と同じ。**"""
    text = DESIGN.read_text(encoding="utf-8")

    assert name not in text, f"docs/DESIGN.md が非公開の {name} を参照している"


def test_design_does_not_narrate_history() -> None:
    """経緯を書かない。

    「以前は X だったが今は Y」「N 周にわたり往復した」「2026-08-15 に起きた」の類は、
    過去形でしか書けないので DESIGN の担当ではない。
    """
    offenders = [(i + 1, line) for i, line in enumerate(lines()) if NARRATIVE.match(line)]

    assert not offenders, "経緯とみられる記述がある:\n" + "\n".join(
        f"  {n}: {line[:78]}" for n, line in offenders[:8]
    )


# --- 公開文書に制御文字を混ぜない -------------------------------------------------

PUBLIC_DOCS = ("README.md", "README.en.md", "docs/DESIGN.md", ".github/SECURITY.md")

#: 生の CR が混じると壊れるテキストを**すべて**さらう。列挙にすると、次に事故が起きる
#: ファイルはたいてい列挙の外にある（実際 ``.gitattributes`` のコメントと、この番人を
#: 置いたテスト自身で起きた）。``.cmd`` は CRLF が正当なので、畳んでから見る。
CR_SENSITIVE = sorted(
    str(path.relative_to(ROOT)).replace("\\", "/")
    for pattern in ("*.md", "*.toml", "*.json", "*.py", "*.yml", ".gitattributes", "bin/*")
    for path in ROOT.glob(pattern)
    if path.is_file()
) + sorted(
    str(path.relative_to(ROOT)).replace("\\", "/")
    for folder in ("src", "tests", "hooks", "docs", ".github", ".claude-plugin")
    for path in (ROOT / folder).rglob("*")
    if path.is_file() and path.suffix in {".py", ".md", ".json", ".yml", ".yaml"}
)


@pytest.mark.parametrize("name", CR_SENSITIVE)
def test_public_documents_have_no_stray_control_characters(name: str) -> None:
    """公開文書に制御文字（タブ・改行以外）を混ぜない。

    シェルのヒアドキュメント越しに文書を書くと、``\r`` が実 CR に、``\n`` が実改行に
    化ける。実際に ``%LOCALAPPDATA%\resource-broker`` が ``%LOCALAPPDATA%`` + CR +
    ``esource-broker`` になり、**掲示板の置き場を案内する行が壊れていた**。
    表示上は気づきにくく、コピーすると動かないパスになる。
    """
    raw = (ROOT / name).read_bytes()

    # **CRLF を先に畳んでから CR を探す。** 単に 0x0D を許すと、守るはずだった事故
    # （CR 1 つが行の途中へ紛れる）がそのまま通る。CRLF は ``.cmd`` などで正当なので
    # 一律には禁じられない。畳んだあとに残る CR だけが事故である。
    raw = raw.replace(b"\r\n", b"\n")

    offenders = [(i, b) for i, b in enumerate(raw) if b < 32 and b not in (9, 10)]
    assert not offenders, (
        f"{name} に制御文字がある（先頭 {offenders[0][0]} バイト目に {offenders[0][1]:#04x}）"
    )


@pytest.mark.parametrize("name", PUBLIC_DOCS)
def test_public_documents_do_not_split_a_windows_path(name: str) -> None:
    """Windows のパスが行をまたいで切れていない。

    ``\r`` の事故は制御文字として残らず**改行に化ける**こともあるので、
    行末が環境変数で終わっていないかを別に見る。
    """
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()

    broken = [
        (i + 1, line)
        for i, line in enumerate(lines)
        if line.rstrip().endswith(("%LOCALAPPDATA%", "%USERPROFILE%", "%TEMP%"))
    ]
    assert not broken, "環境変数の直後でパスが切れている:\n" + "\n".join(
        f"  {n}: {line[-50:]}" for n, line in broken
    )


# --- self-hosted runner に fork の pull request を通さない -------------------------


def test_a_self_hosted_runner_never_accepts_a_fork_pull_request() -> None:
    """self-hosted を使う job は、fork からの pull request を必ず弾く。

    公開リポジトリで self-hosted runner に fork のコードを渡すと、**pull request を
    投げるだけで作者のマシン上で任意のコードが走る**（``pyproject.toml`` の解決も
    ``tests/*.py`` も）。GitHub 自身が警告している構成である。

    「公開したら GitHub ホストへ移す」という手順に頼らない。手順は忘れられるし、
    忘れたことが分かるのは踏まれた後である。公開の瞬間に危険になる設定は、
    private のうちから置かない。

    **限界を書いておく。** self-hosted runner をカスタムラベル（``runs-on: pi-cm5``）で
    登録すると、この番人からは見えない。「番人があるから安全」とは読まないこと。
    """
    guard = "github.event.pull_request.head.repo.full_name == github.repository"

    workflows = sorted(
        list((ROOT / ".github" / "workflows").glob("*.yml"))
        + list((ROOT / ".github" / "workflows").glob("*.yaml"))
    )
    for path in workflows:
        # コメントは落とす。**この workflow は自分の危険をコメントで説明している**ので、
        # 素朴に探すと説明文そのものに反応する。
        lines = [
            "" if line.lstrip().startswith("#") else line
            for line in path.read_text(encoding="utf-8").split("\n")
        ]
        if not any("self-hosted" in line for line in lines):
            continue

        # **トリガごと禁じる。** `pull_request_target` は fork の PR で走りながら
        # 書き込み権限を持つ。self-hosted と組み合わせてよい理由が無い。
        assert not any(line.strip().startswith("pull_request_target:") for line in lines), (
            f"{path.name}: self-hosted と pull_request_target を同居させている"
        )

        # **job 単位で見る。** ファイルのどこかに guard があるだけでは、それが
        # self-hosted の job に付いているかどうか分からない。
        for i, line in enumerate(lines):
            if "self-hosted" not in line:
                continue
            start = next(
                (j for j in range(i, -1, -1) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[j])),
                None,
            )
            assert start is not None, f"{path.name}:{i + 1} の job が見つからない"
            assert any(guard in lines[j] for j in range(start, i)), (
                f"{path.name}:{i + 1} の self-hosted job が fork の pull request を弾いていない"
            )


# --- 版番号は 3 か所に手書きされている ---------------------------------------------


def test_the_version_is_the_same_everywhere() -> None:
    """``__init__.py`` を基準に ``plugin.json`` / ``marketplace.json`` の版が一致している。

    ``pyproject.toml`` は動的版（``[tool.hatch.version]``）に移したため、もう手書きの
    正本ではない。**基準は ``__init__.py`` の ``__version__``** —— Python の実行時世界と
    プラグインカタログの世界はビルド段を挟まずには繋がらないため（issue #12）、
    ``plugin.json`` / ``marketplace.json`` はそれぞれ独立に手で合わせるしかない。
    ``__init__.py`` は一度この検査の対象から漏れて ``0.1.0`` のまま取り残されたことがある。
    """
    # __init__.py は import せず、テキストとして読む。import すると、この検査だけが
    # パッケージを import できる前提を増やす。
    init_py = (ROOT / "src" / "resource_broker" / "__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__ = "([^"]+)"', init_py, re.M)
    assert init_match, "__init__.py に __version__ が無い"
    expected = init_match.group(1)

    for name in ("plugin.json", "marketplace.json"):
        data = json.loads((ROOT / ".claude-plugin" / name).read_text(encoding="utf-8"))
        found = data.get("version") or data["plugins"][0]["version"]
        assert found == expected, f"{name} の版 {found} が __init__.py の {expected} と違う"


def test_pyproject_has_no_hand_written_version() -> None:
    """``pyproject.toml`` に ``version = "..."`` を書き戻していない。

    動的版（``[tool.hatch.version]``、正本は ``__init__.py``）へ移した後に誰かが
    ``version = "..."`` を書き足すと、静かに 2 つ目の正本ができる——一方だけが
    更新されて他方が腐っても、この検査群は気づけない。書けば必ずここで落とす。
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not re.search(r'^version = "', pyproject, re.M), (
        "pyproject.toml に version が書き戻されている。"
        "版の正本は src/resource_broker/__init__.py の __version__ である"
    )


# --- README が数えている件数と実装を一致させる ---------------------------------------


def test_the_readmes_state_the_real_number_of_subprocess_calls() -> None:
    """README の「``subprocess`` は N か所」が実装と一致している。

    **これは 2 周連続で嘘になった。** 1 度目は ``taskkill`` を数え落とし、2 度目は
    ログを開けなかったときの退避路を足した自分の修正で 4 が 5 になった。
    README 自身が「審査や監査で最初に確かめられる性質」と前置きしている行であり、
    ``grep`` 一発で嘘が見つかる。**散文が実装を追い越せないように機械で縛る。**
    """
    pattern = re.compile(r"subprocess\.(run|Popen)\(")
    count = sum(
        1
        for folder in ("src", "hooks")
        for path in (ROOT / folder).rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if pattern.search(line)
    )

    assert count > 0, "数え方が壊れている"
    for name in ("README.md", "README.en.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert f"subprocess` は {count} か所" in text or f"in exactly {count} places" in text, (
            f"{name} が {count} 以外の件数を書いている"
        )


# --- 掲示板は glob で読まない -------------------------------------------------------


def test_the_board_is_never_read_with_glob() -> None:
    """掲示板の走査に ``Path.glob`` を使わない。

    ``glob`` は ``OSError`` を**内部で握り潰して空を返す**。掲示板が通常ファイルに
    なっている・権限で拒否されている・切断されたネットワークパスを指している——
    いずれもが「空の掲示板」と同じ形になり、**実際には使われている資源を全セッションへ
    「空き」として配る**。

    これは 2 周にわたって踏んだ。1 度目は気づかず、2 度目は 1 箇所だけ直して
    「対処済み」に見える状態を作った（主経路の 3 箇所が残っていた）。**共有の走査を
    1 つに寄せ、glob へ戻る道を塞ぐ。**

    ログと監査は対象外である（読めなくても「使用中の資源が空きに見える」に
    つながらない）。
    """
    offenders = []
    for folder in ("src", "hooks"):
        for path in (ROOT / folder).rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "glob(" in line and "*.json" in line and "*.jsonl" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")

    assert not offenders, "掲示板を glob で読んでいる: " + ", ".join(offenders)


# --- 消した仕組みの名前が文書に残らない -------------------------------------------


def test_public_documents_do_not_mention_removed_machinery() -> None:
    """削除した仕組みの名前が公開文書に残っていない。

    仕様書が正本だと言っている以上、**存在しないコマンドや関数を説明したまま**にする
    のは看板の問題である。実装の関数名と突き合わせるより、消した名前の禁止のほうが
    保守が楽で、実際に起きた取り残しは全部これで拾える。
    """
    gone = (
        "try_claim",
        "rb join",
        "--primary",
        "add_join",
        "find_own_join",
        "JoinResult",
        # issue #9: 資源引数を受け取る `rb status`、表示名を併記する仕組み、
        # 中身の照合で消す旧形式の削除経路（消した理由は本文が別に説明する）。
        "--display",
        "naming.label",
        "_remove_matching",
    )
    for name in ("docs/DESIGN.md", "README.md", "README.en.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for word in gone:
            assert word not in text, f"{name} に消した仕組みの名前が残っている: {word}"
