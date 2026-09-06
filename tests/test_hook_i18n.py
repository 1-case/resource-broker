"""フックの文言（日英併記）と言語判定を守る番人。

issue #13 第 1 段の方針（``docs/DESIGN.md`` 「Hook Spec」）はこうである。

    **日本語が正本、英語は訳。** 英語訳は利用者を増やすためであって、正本を
    入れ替えるためではない。**訳が欠けたら日本語で動く**——この性質を構造で
    保証する。

構造で保証する場所は 2 つ。

1. 各フックの ``tr()`` が ``en`` の欠落を ``ja`` へ読み替える（実装）
2. ここにある 2 本の番人が、``ja`` と ``en`` の**形**が揃っていることを守る
   （鍵集合・書式指定子）。揃っていなければ ``tr()`` の読み替えは効いても、
   **書式指定子がずれた ``en`` はそのまま ``KeyError`` で落ちる**——揃って
   いることを検査で固定する必要があるのはここだけである

加えて、言語判定（4 段のフォールバック）が各段で正しく次へ委ねることも
ここで確かめる。フックは他プロジェクトから素の ``python`` で単体起動される
ため、この判定・文言表は 3 つのフックへ意図的に重複させてある
（:func:`hooks.sessionstart_notice.clip` と同じ理由）。3 つとも同じ形で
壊れうるので、3 つとも同じ番人にかける。
"""

from __future__ import annotations

import json
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT / "hooks"

#: 3 つのフック本体。文言表と言語判定はこの全てに重複させてある。
HOOK_FILES = [
    "sessionstart_notice.py",
    "prompt_board_reminder.py",
    "pretooluse_notice.py",
]

#: ``{名前}`` 形式の書式指定子だけを拾う（本プロジェクトの文言表はどれも
#: 名前付きプレースホルダのみを使い、``{}`` や ``{0}`` は使わない）。
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load_hook_module(name: str) -> ModuleType:
    """フックを import して定数・関数を読む（本体は import しない単体スクリプトのため）。"""
    spec = spec_from_file_location(name, HOOKS_DIR / name)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def placeholders(text: str) -> frozenset[str]:
    """テンプレート中の ``{名前}`` の集合（順序も個数も畳んで名前の集合にする）。"""
    return frozenset(PLACEHOLDER.findall(text))


# --- 番人 1: ja / en の鍵集合が一致する -----------------------------------------


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_every_message_has_a_japanese_original(hook_name: str) -> None:
    """全てのメッセージが ``ja``（正本）を持つ。**正本が無ければ何も保証できない。**"""
    module = load_hook_module(hook_name)
    missing = [key for key, table in module.MESSAGES.items() if "ja" not in table]
    assert not missing, f"{hook_name}: ja が無いメッセージ: {missing}"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_message_table_entries_hold_only_ja_and_en(hook_name: str) -> None:
    """メッセージ表の 1 件は ``{"ja": ..., "en": ...}`` の形だけを持つ。

    ``en`` を書き忘れても ``tr()`` は ``ja`` へ読み替えるので実行時には気づけない。
    ここで鍵の集合そのものを固定し、``ja`` しか無い（訳がまだ無い）ことと、
    ``ja`` / ``en`` 以外の鍵が紛れ込む（typo）ことの両方を検出する。
    """
    module = load_hook_module(hook_name)
    offenders = {
        key: sorted(table)
        for key, table in module.MESSAGES.items()
        if not set(table) <= {"ja", "en"}
    }
    assert not offenders, f"{hook_name}: ja/en 以外の鍵を持つメッセージ: {offenders}"


# --- 番人 2: 書式指定子が ja / en で一致する -------------------------------------


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_format_placeholders_match_between_japanese_and_english(hook_name: str) -> None:
    """``{名前}`` の名前と個数が ``ja`` と ``en`` で一致する。

    **これが番人の要である。** ``tr()`` は ``template.format(**kwargs)`` を呼ぶ。
    ``en`` に無い名前を ``ja`` が使っていれば ``en`` を選んだ側で ``KeyError``、
    逆に ``en`` だけが余分な名前を使っていれば ``ja`` を選んだ側で同じく
    ``KeyError`` になる——**どちらの言語を選んでも実行時に落ちる形の退行**であり、
    読む・書くだけでは気づけない（英語が読めれば見た目は自然な文になる）。
    """
    module = load_hook_module(hook_name)
    offenders = []
    for key, table in module.MESSAGES.items():
        ja_names = placeholders(table["ja"])
        en_names = placeholders(table.get("en", table["ja"]))
        if ja_names != en_names:
            offenders.append(f"{hook_name}:{key} ja={sorted(ja_names)} en={sorted(en_names)}")
    assert not offenders, "書式指定子が ja/en でずれている:\n" + "\n".join(offenders)


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_tr_falls_back_to_japanese_when_english_is_missing(hook_name: str) -> None:
    """``en`` が欠けたメッセージは ``tr(..., "en")`` でも ``ja`` を返す。

    「訳が欠けたら日本語で動く」という設計上の約束を、``tr()`` の実装として固定する。
    """
    module = load_hook_module(hook_name)
    module.MESSAGES["_test_only_no_english"] = {"ja": "日本語だけの文言"}
    try:
        assert module.tr("_test_only_no_english", "en") == "日本語だけの文言"
    finally:
        del module.MESSAGES["_test_only_no_english"]


# --- 言語判定: 4 段のどこからでも正しく次へ委ねる --------------------------------


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_default_is_japanese_when_nothing_is_known(hook_name: str) -> None:
    """4 段とも確定しなければ日本語（正本）に落ちる。"""
    module = load_hook_module(hook_name)
    assert module.choose_language() == "ja"
    assert module.choose_language(None, None, None) == "ja"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_the_explicit_env_var_wins_over_every_other_stage(hook_name: str) -> None:
    """段 1（環境変数）が確定していれば、後続の段は見ない。"""
    module = load_hook_module(hook_name)
    assert module.choose_language("en", "ja", "ja") == "en"
    assert module.choose_language("ja", "en", "en") == "ja"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_claude_code_setting_wins_when_the_env_var_is_absent(hook_name: str) -> None:
    """段 1 が None のとき、段 2（Claude Code の設定）が採られる。"""
    module = load_hook_module(hook_name)
    assert module.choose_language(None, "en", "ja") == "en"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_os_locale_wins_when_the_first_two_stages_are_absent(hook_name: str) -> None:
    """段 1・2 が None のとき、段 3（OS のロケール）が採られる。"""
    module = load_hook_module(hook_name)
    assert module.choose_language(None, None, "en_US.UTF-8") == "en"
    assert module.choose_language(None, None, "Japanese_Japan.932") == "ja"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_unrecognized_values_are_skipped_not_treated_as_a_decision(hook_name: str) -> None:
    """判断がつかない値（``fr`` 等）は確定とみなさず、次の段へ落ちる。"""
    module = load_hook_module(hook_name)
    assert module.choose_language("fr", "de", "en") == "en"
    assert module.choose_language("fr", "de", "it") == "ja"  # 全段とも不明なら既定


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_detect_language_reads_the_env_var_via_injection(hook_name: str, tmp_path: Path) -> None:
    """:func:`detect_language` が実際に環境変数の段を読む（I/O 込みの結線を確かめる）。"""
    module = load_hook_module(hook_name)
    missing_settings = tmp_path / "missing.json"

    assert (
        module.detect_language(env={"RESOURCE_BROKER_LANG": "en"}, settings_path=missing_settings)
        == "en"
    )
    assert (
        module.detect_language(env={"RESOURCE_BROKER_LANG": "ja"}, settings_path=missing_settings)
        == "ja"
    )


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_detect_language_reads_claude_code_settings_via_injection(
    hook_name: str, tmp_path: Path
) -> None:
    """:func:`detect_language` が実際に ``~/.claude/settings.json`` の段を読む。"""
    module = load_hook_module(hook_name)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"language": "en"}), encoding="utf-8")

    assert module.detect_language(env={}, settings_path=settings) == "en"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_a_broken_claude_code_settings_file_falls_through_to_the_next_stage(
    hook_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """壊れた設定ファイルでも例外を出さず、次の段（OS ロケール）へ落ちる。

    **どの段でも例外を出さない。** 設定ファイルが読めない・壊れているは、
    フックが黙る経路を作ってはならない。
    """
    module = load_hook_module(hook_name)
    broken = tmp_path / "settings.json"
    broken.write_text("{ これは JSON ではない", encoding="utf-8")
    monkeypatch.setattr(module, "_os_locale_lang", lambda: "en")

    assert module.detect_language(env={}, settings_path=broken) == "en"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_detect_language_falls_back_to_os_locale_via_injection(
    hook_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """段 1・2 が無いとき、:func:`detect_language` は実際に段 3 を読みに行く。"""
    module = load_hook_module(hook_name)
    missing_settings = tmp_path / "missing.json"
    monkeypatch.setattr(module, "_os_locale_lang", lambda: "en")

    assert module.detect_language(env={}, settings_path=missing_settings) == "en"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_detect_language_defaults_to_japanese_when_every_stage_fails(
    hook_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 段とも取れなければ日本語（正本）に落ちる——実際の呼び出し経路で確かめる。"""
    module = load_hook_module(hook_name)
    missing_settings = tmp_path / "missing.json"
    monkeypatch.setattr(module, "_os_locale_lang", lambda: None)

    assert module.detect_language(env={}, settings_path=missing_settings) == "ja"


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_an_exploding_locale_probe_does_not_crash_detection(
    hook_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OS ロケールの取得が例外を出しても、判定全体は落ちずに既定へ倒れる。

    ``locale`` はプラットフォームや壊れたロケール名で例外を出すことがある。
    **どの段でも例外を出さない**という約束を、実際に例外を注入して確かめる。
    """
    module = load_hook_module(hook_name)

    def boom() -> str | None:
        raise RuntimeError("ロケールが壊れている")

    monkeypatch.setattr(module, "_os_locale_lang", boom)
    missing_settings = tmp_path / "missing.json"

    assert module.detect_language(env={}, settings_path=missing_settings) == "ja"


# --- normalize_lang: 値の正規化 --------------------------------------------------


@pytest.mark.parametrize("hook_name", HOOK_FILES)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ja", "ja"),
        ("JA", "ja"),
        ("japanese", "ja"),
        ("日本語", "ja"),
        ("ja_JP.UTF-8", "ja"),
        ("Japanese_Japan.932", "ja"),
        ("en", "en"),
        ("EN", "en"),
        ("english", "en"),
        ("en_US.UTF-8", "en"),
        ("English_United States.1252", "en"),
        ("", None),
        ("fr", None),
        ("zh_CN", None),
        (None, None),
        (42, None),
    ],
)
def test_normalize_lang_maps_known_forms_and_gives_up_on_the_rest(
    hook_name: str, raw: object, expected: str | None
) -> None:
    """既知の表記は ja/en に正規化し、判断がつかない値は None にする。"""
    module = load_hook_module(hook_name)
    assert module.normalize_lang(raw) == expected
