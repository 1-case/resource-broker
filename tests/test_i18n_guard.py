"""``src/`` 側の文言表（``resource_broker.messages``）と、4 つの言語判定実装が
そろっていることを守る番人（issue #13 第 2 段）。

``tests/test_hook_i18n.py``（第 1 段。3 つのフック）と役割を分けている。

1. ここでは ``resource_broker.messages`` 単体の番人を持つ
   （``ja`` の存在・``ja``/``en`` 以外の鍵の禁止・書式指定子の一致・
   ``en`` 欠落時の ``ja`` フォールバック）。**形は ``test_hook_i18n.py`` と同じ**
   ――フックと ``src/`` で番人の基準がずれると、片方だけ緩んだ表が紛れ込む。

2. **4 つの言語判定実装（フック 3 つ + ``src/resource_broker/messages``）が、
   同じ入力に同じ答えを返すこと**を横断的に確かめる。``src/`` の中は import
   できるので文言表は 1 つにまとめたが（``docs/DESIGN.md``「Hook Spec」参照）、
   言語判定だけは「フックから見えない独立した実装」として引き続き 4 つ目の
   写しになっている（フックは互いに import できないため）。データの一致
   （鍵・書式指定子）を守るだけでは、**判定ロジックそのものが 1 つだけ
   ずれる**という壊れ方は捕まらない――ここではそれを実際の呼び出しで確かめる。
"""

from __future__ import annotations

import json
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

from resource_broker import messages as src_messages

ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT / "hooks"

#: 3 つのフック本体。言語判定はこの全てと ``resource_broker.messages`` に重複させてある。
HOOK_FILES = [
    "sessionstart_notice.py",
    "prompt_board_reminder.py",
    "pretooluse_notice.py",
]

#: 4 実装すべての名前（比較の出力をどれが失敗したか分かる形にするため）。
IMPLEMENTATIONS = [*HOOK_FILES, "resource_broker.messages"]

#: ``{名前}`` 形式の書式指定子だけを拾う。
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load_hook_module(name: str) -> ModuleType:
    """フックを import して定数・関数を読む（本体は import しない単体スクリプトのため）。"""
    spec = spec_from_file_location(name, HOOKS_DIR / name)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def placeholders(text: str) -> frozenset[str]:
    """テンプレート中の ``{名前}`` の集合。"""
    return frozenset(PLACEHOLDER.findall(text))


# --- 番人 1: resource_broker.messages 単体（test_hook_i18n.py と同じ基準） -----------


def test_every_message_has_a_japanese_original() -> None:
    """全てのメッセージが ``ja``（正本）を持つ。**正本が無ければ何も保証できない。**"""
    missing = [key for key, table in src_messages.MESSAGES.items() if "ja" not in table]
    assert not missing, f"ja が無いメッセージ: {missing}"


def test_message_table_entries_hold_only_ja_and_en() -> None:
    """メッセージ表の 1 件は ``{"ja": ..., "en": ...}`` の形だけを持つ。

    ``en`` を書き忘れても ``tr()`` は ``ja`` へ読み替えるので実行時には気づけない。
    ここで鍵の集合そのものを固定し、``ja`` しか無い（訳がまだ無い）ことと、
    ``ja`` / ``en`` 以外の鍵が紛れ込む（typo）ことの両方を検出する。
    """
    offenders = {
        key: sorted(table)
        for key, table in src_messages.MESSAGES.items()
        if not set(table) <= {"ja", "en"}
    }
    assert not offenders, f"ja/en 以外の鍵を持つメッセージ: {offenders}"


def test_format_placeholders_match_between_japanese_and_english() -> None:
    """``{名前}`` の名前と個数が ``ja`` と ``en`` で一致する。

    **これが番人の要である。** ``tr()`` は ``template.format(**kwargs)`` を呼ぶ。
    ``en`` に無い名前を ``ja`` が使っていれば ``en`` を選んだ側で ``KeyError``、
    逆に ``en`` だけが余分な名前を使っていれば ``ja`` を選んだ側で同じく
    ``KeyError`` になる——**どちらの言語を選んでも実行時に落ちる形の退行**であり、
    読む・書くだけでは気づけない（英語が読めれば見た目は自然な文になる）。
    """
    offenders = []
    for key, table in src_messages.MESSAGES.items():
        ja_names = placeholders(table["ja"])
        en_names = placeholders(table.get("en", table["ja"]))
        if ja_names != en_names:
            offenders.append(f"{key} ja={sorted(ja_names)} en={sorted(en_names)}")
    assert not offenders, "書式指定子が ja/en でずれている:\n" + "\n".join(offenders)


def test_tr_falls_back_to_japanese_when_english_is_missing() -> None:
    """``en`` が欠けたメッセージは ``tr()`` でも ``ja`` を返す。

    「訳が欠けたら日本語で動く」という設計上の約束を、``tr()`` の実装として固定する。
    """
    src_messages.use_language("en")
    src_messages.MESSAGES["_test_only_no_english"] = {"ja": "日本語だけの文言"}
    try:
        assert src_messages.tr("_test_only_no_english") == "日本語だけの文言"
    finally:
        del src_messages.MESSAGES["_test_only_no_english"]


def test_tr_formats_keyword_arguments() -> None:
    """``tr()`` はキーワード引数でテンプレートを埋める。"""
    src_messages.use_language("ja")
    src_messages.MESSAGES["_test_only_placeholder"] = {"ja": "{name} 個", "en": "{name} items"}
    try:
        assert src_messages.tr("_test_only_placeholder", name=3) == "3 個"
        src_messages.use_language("en")
        assert src_messages.tr("_test_only_placeholder", name=3) == "3 items"
    finally:
        del src_messages.MESSAGES["_test_only_placeholder"]


# --- 番人 2: 4 実装（フック 3 つ + resource_broker.messages）が同じ判定をする ----------
#
# 文言表は 1 つにまとめたが、言語判定（``choose_language`` / ``normalize_lang`` /
# ``detect_language``）はフックから import できないため、依然として 4 つの独立した
# 写しである。**写し同士が同じ入力に同じ答えを返すこと**をここで固定する。


def _all_implementations() -> dict[str, ModuleType]:
    modules: dict[str, ModuleType] = {name: load_hook_module(name) for name in HOOK_FILES}
    modules["resource_broker.messages"] = src_messages
    return modules


@pytest.mark.parametrize(
    "values",
    [
        (),
        (None, None, None),
        ("en", "ja", "ja"),
        ("ja", "en", "en"),
        (None, "en", "ja"),
        (None, None, "en_US.UTF-8"),
        (None, None, "Japanese_Japan.932"),
        ("fr", "de", "en"),
        ("fr", "de", "it"),
        ("日本語", None, None),
        ("EN", None, None),
        ("", "en", None),
    ],
)
def test_choose_language_agrees_across_all_four_implementations(
    values: tuple[object, ...],
) -> None:
    """4 実装の ``choose_language`` が同じ入力に同じ答えを返す。"""
    modules = _all_implementations()
    results = {name: module.choose_language(*values) for name, module in modules.items()}
    assert len(set(results.values())) == 1, f"choose_language{values} が実装間でずれた: {results}"


@pytest.mark.parametrize(
    "raw",
    [
        "ja",
        "JA",
        "japanese",
        "日本語",
        "ja_JP.UTF-8",
        "Japanese_Japan.932",
        "en",
        "EN",
        "english",
        "en_US.UTF-8",
        "English_United States.1252",
        "",
        "fr",
        "zh_CN",
        None,
        42,
    ],
)
def test_normalize_lang_agrees_across_all_four_implementations(raw: object) -> None:
    """4 実装の ``normalize_lang`` が同じ入力に同じ答えを返す。"""
    modules = _all_implementations()
    results = {name: module.normalize_lang(raw) for name, module in modules.items()}
    assert len(set(results.values())) == 1, f"normalize_lang({raw!r}) が実装間でずれた: {results}"


def test_detect_language_agrees_across_all_four_implementations_env_wins(tmp_path: Path) -> None:
    """段 1（環境変数）が確定しているとき、4 実装が同じ結果を返す。"""
    modules = _all_implementations()
    missing_settings = tmp_path / "missing.json"
    results = {
        name: module.detect_language(
            env={"RESOURCE_BROKER_LANG": "en"}, settings_path=missing_settings
        )
        for name, module in modules.items()
    }
    assert len(set(results.values())) == 1, f"detect_language が実装間でずれた: {results}"
    assert set(results.values()) == {"en"}


def test_detect_language_env_beats_settings_across_all_four_implementations(
    tmp_path: Path,
) -> None:
    """段 1 と段 2 が**食い違って両方とも確定している**とき、4 実装とも段 1 を採る。

    上の ``..._env_wins`` は段 2 を「無い」（``missing_settings``）にしてあるため、
    ``detect_language`` 内部で段 1 と段 2 を呼ぶ**順序**を入れ替えても
    ``choose_language`` に渡る非 None の値は 1 つしかなく、この壊れ方を検出できない
    ——実際に注入して確かめた（issue #13 第 2 段の作業ログ参照）。ここでは両方を
    明示的に確定させ、**優先順位そのものが正しく配線されている**ことを固定する。
    """
    modules = _all_implementations()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"language": "ja"}), encoding="utf-8")
    results = {
        name: module.detect_language(env={"RESOURCE_BROKER_LANG": "en"}, settings_path=settings)
        for name, module in modules.items()
    }
    assert len(set(results.values())) == 1, f"detect_language が実装間でずれた: {results}"
    assert set(results.values()) == {"en"}, f"段 1 が段 2 に負けた実装がある: {results}"


def test_detect_language_agrees_across_all_four_implementations_settings_wins(
    tmp_path: Path,
) -> None:
    """段 2（Claude Code の設定）が確定しているとき、4 実装が同じ結果を返す。"""
    modules = _all_implementations()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"language": "en"}), encoding="utf-8")
    results = {
        name: module.detect_language(env={}, settings_path=settings)
        for name, module in modules.items()
    }
    assert len(set(results.values())) == 1, f"detect_language が実装間でずれた: {results}"
    assert set(results.values()) == {"en"}


def test_detect_language_agrees_across_all_four_implementations_default(tmp_path: Path) -> None:
    """4 段とも取れないとき、4 実装が同じ既定（日本語）に落ちる。"""
    modules = _all_implementations()
    missing_settings = tmp_path / "missing.json"
    results = {}
    for name, module in modules.items():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(module, "_os_locale_lang", lambda: None)
            results[name] = module.detect_language(env={}, settings_path=missing_settings)
        finally:
            mp.undo()
    assert len(set(results.values())) == 1, f"detect_language が実装間でずれた: {results}"
    assert set(results.values()) == {"ja"}
