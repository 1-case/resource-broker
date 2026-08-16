"""``docs/DESIGN.md`` が「最終到達状態だけ」に留まっていることを機械で守る。

文書が膨らむのは、たいてい**経緯が混ざる**からである。1 つの判断について「何を決めたか」は
数行で済むが、「どう間違えて、どう気づいたか」は物語になる。両方を同じ文書に置くと、
読む側は仕様を探すのに物語を読まされ、書く側は改訂のたびにどちらを直すか迷う。

線引きは 1 つ。**「この文は将来も真か」**。真であり続けるなら仕様か根拠なので DESIGN へ、
過去形でしか書けないなら経緯なので ``EXPERIMENTS.md``（非公開の作業ログ）へ。

「注意する」だけでは守れないので、上限と混入をここで固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md"

#: 行数の上限。増えたぶんはたいてい経緯である。
MAX_LINES = 600

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
