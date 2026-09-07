"""``src/`` 側で使う固定文言の一括管理（issue #13 第 2 段）。

**日本語が正本、英語は訳。** 英語訳は利用者を増やすためであって、正本を入れ替える
ためではない。**訳が欠けたら日本語で動く**——``tr()`` が ``en`` の欠落を ``ja`` へ
読み替えるので、訳が追いつかなくても日本語で必ず動く。

フック（``hooks/*.py``）は互いに import できないため各自が文言表と言語判定を
持つ（3 つの重複）。**``src/`` の中は import できる**ので、``cli.py`` /
``board.py`` / ``runner.py`` / ``liveness.py`` / ``naming.py`` はここ 1 つの表を
共有する——本パッケージの中に 5 つ目の重複を作らない。

言語判定（:func:`detect_language` とその 4 段）だけは、それでも**フックからは
見えない独立した実装**として持つ。フックは素の ``python`` で単体起動され、
本パッケージが入っている前提を置けないため、``src/`` 側の実装をフックから
import することはできない——結果としてここが 4 つ目の写しになる（フック 3 つ +
ここ）。**写し同士が同じ入力に同じ答えを返すことは、``tests/test_i18n_guard.py``
の横断番人が守る。**
"""

from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path

ENCODING = "utf-8"

# --- 言語の判定 ----------------------------------------------------------------
#
# 判定は次の 4 段を上から順に試す。
#
#   1. 環境変数 RESOURCE_BROKER_LANG（明示は暗黙に勝つ）
#   2. Claude Code の language（``~/.claude/settings.json``）
#   3. OS のロケール
#   4. どれも分からなければ日本語
#
# フック 3 本（``hooks/pretooluse_notice.py`` 等）に意図的に重複させてある実装と
# **同じ形**にすること。ここだけ違う判定になると、CLI とフックが同じ環境で
# 別の言語を選ぶという壊れ方をする。

#: 明示的な言語指定。設定しなければ次の段へ進む。
LANG_ENV = "RESOURCE_BROKER_LANG"


def normalize_lang(raw: object) -> str | None:
    """言語を表す値を ``ja`` / ``en`` に正規化する。**判断がつかなければ None。**

    ``ja`` / ``日本語`` はそのまま日本語、``en`` / ``english`` は英語と認める。
    OS のロケール文字列（``ja_JP.UTF-8`` や ``English_United States``）は
    区切り記号の前の主要部分だけを見て同じ表に当てる——**ここで例外を出さない**
    ことが 4 段のどこからでも安全に呼べる条件である。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if "日本語" in text:
        return "ja"
    token = text.replace("-", "_").replace(".", "_").split("_")[0]
    if token in ("ja", "japanese"):
        return "ja"
    if token in ("en", "english"):
        return "en"
    return None


def choose_language(*values: object) -> str:
    """複数の生値から、最初に確定した言語を採る。**上から順に、明示が暗黙に勝つ。**

    ここは純粋関数——I/O を一切行わないので、境界値をそのまま渡してテストできる。
    全て確定しなければ日本語（正本）に落ちる。
    """
    for value in values:
        normalized = normalize_lang(value)
        if normalized:
            return normalized
    return "ja"


def _env_lang(env: dict[str, str] | None = None) -> str | None:
    """環境変数 :data:`LANG_ENV` を読む。"""
    source = env if env is not None else os.environ
    return source.get(LANG_ENV)


def _claude_code_lang(settings_path: Path | None = None) -> str | None:
    """``~/.claude/settings.json`` の ``"language"`` を読む。読めなければ None。

    ファイルが無い・壊れている・型が違う、いずれも例外を外へ出さず None を返す
    （次の段へ落とすため）。
    """
    target = settings_path
    if target is None:
        target = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(target.read_text(encoding=ENCODING))
    except (OSError, ValueError):
        return None
    value = data.get("language") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _windows_ui_lang() -> str | None:
    """Windows の UI 言語を ``GetUserDefaultUILanguage`` の primary language ID から読む。

    LCID の下位 10 bit が primary language ID である（0x11 = 日本語, 0x09 = 英語）。
    ``ctypes`` の戻り値の型は必ず宣言する——既定の ``c_int`` のままだと符号付きに
    解釈され、大きい LCID で負値化しうる。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetUserDefaultUILanguage.restype = ctypes.c_ushort
        lcid = kernel32.GetUserDefaultUILanguage()
    except Exception:  # noqa: BLE001 - fail-open。取れなければ次の段へ
        return None
    primary = lcid & 0x3FF
    if primary == 0x11:
        return "ja"
    if primary == 0x09:
        return "en"
    return None


def _os_locale_lang() -> str | None:
    """OS のロケールから言語を推定する。Windows は UI 言語を優先する。

    ``locale.getlocale()`` は環境によって ``ValueError`` を投げることがある
    （壊れたロケール名）。**ここで拾い、None へ落として次の段へ渡す。**
    """
    if sys.platform == "win32":
        found = _windows_ui_lang()
        if found:
            return found
    try:
        code = locale.getlocale()[0]
    except (ValueError, TypeError):
        code = None
    if not code:
        code = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    return code


def detect_language(
    *, env: dict[str, str] | None = None, settings_path: Path | None = None
) -> str:
    """4 段の判定をまとめて実行する。**I/O を行うのはここだけ。**

    判定ロジック自体は :func:`choose_language`（純関数）に任せ、ここは
    「どこから値を取ってくるか」だけを受け持つ。**どの段が失敗しても例外を
    外へ出さない**（fail-open。CLI が黙る経路を作らない）。
    """

    def safe(probe: object) -> str | None:
        try:
            return probe()  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - fail-open。次の段へ落とす
            return None

    return choose_language(
        safe(lambda: _env_lang(env)),
        safe(lambda: _claude_code_lang(settings_path)),
        safe(_os_locale_lang),
    )


# --- 現在の言語（プロセス内で 1 度だけ判定し、以後はここから読む） ------------------
#
# ``tr()`` を呼ぶたびに :func:`detect_language` を呼び直すと、``cli.py`` 1 回の
# 実行で何度も設定ファイルを読みに行く（1 コマンドあたり数十件のメッセージがある）。
# **エントリポイント（``cli.main()``）が起動直後に 1 度だけ** :func:`use_language`
# で確定させ、以後はここに積んだ値を読むだけにする。
#
# **モジュールグローバルだが、テスト間で腐らない。** ``main()`` は呼ばれるたびに
# 必ず :func:`use_language` を呼び直す（キャッシュを使い回さない）ので、
# 同一プロセス内で複数回 ``main()`` を呼ぶテスト（``tests/conftest.py`` が
# ``RESOURCE_BROKER_LANG`` を環境変数で切り替える）でも前回の値を引きずらない。
_current_lang: str | None = None


def use_language(lang: str) -> None:
    """以後の :func:`tr` が使う言語を確定させる。``main()`` の先頭で必ず呼ぶこと。"""
    global _current_lang
    _current_lang = lang


def current_language() -> str:
    """いま有効な言語。:func:`use_language` が一度も呼ばれていなければ判定する。

    ``main()`` を経由しない呼び出し（ライブラリとしての直接利用）でも
    ``tr()`` が例外にならないための保険であり、通常経路では常に
    :func:`use_language` が先に効いている。
    """
    global _current_lang
    if _current_lang is None:
        _current_lang = detect_language()
    return _current_lang


#: 固定文言の表。**日本語が正本、英語は訳。**
#:
#: ``{キー: {"ja": "…", "en": "…"}}`` の形で日英を隣に並べる——ズレが目で見える。
#: **`en` が欠けていれば `ja` を返す**（:func:`tr`）ので、訳が追いつかなくても
#: 日本語で必ず動く。宣言の自由記述（``--job`` / ``--observed`` / ``--sharing``）は
#: 訳す対象ではない——書いた人の言語のまま出す。
MESSAGES: dict[str, dict[str, str]] = {
    # --- liveness.py: 判定理由（Verdict → 1 行） ---------------------------------
    "verdict_free": {
        "ja": "掲示板にエントリが無い",
        "en": "No entry on the board",
    },
    "verdict_held": {
        "ja": "実測で使用を確認、または宣言が有効",
        "en": "Confirmed in use by direct observation, or an active declaration exists",
    },
    "verdict_stale_probe": {
        "ja": "猶予を過ぎ、実測が空きで宣言プロセスも消えている（幽霊）",
        "en": (
            "Past the grace period, observed free, and the declaring process is gone (a ghost)"
        ),
    },
    "verdict_stale_reboot": {
        "ja": "宣言が再起動より前のもの（確定的な幽霊）",
        "en": "The declaration predates the last reboot (a confirmed ghost)",
    },
    "verdict_uncertain": {
        "ja": "宣言はあるが裏が取れない（PID 消失または時刻不正）",
        "en": (
            "There is a declaration, but it cannot be corroborated (the PID no "
            "longer exists or the timestamp is invalid)"
        ),
    },
    # --- naming.py ---------------------------------------------------------------
    "resource_id_empty": {
        "ja": "資源 ID が空である",
        "en": "The resource ID is empty",
    },
    # --- runner.py -----------------------------------------------------------------
    "log_limit_reached": {
        "ja": "ログが上限（{limit} バイト）に達したため、以降の出力を破棄した（ジョブは継続中）",
        "en": (
            "The log hit its limit ({limit} bytes); further output is being discarded "
            "(the job keeps running)"
        ),
    },
    "log_write_failed": {
        "ja": "警告: ログに書けなくなりました（{error}）。ジョブは継続します",
        "en": "Warning: the log can no longer be written to ({error}). The job keeps running",
    },
    "actual_executable": {
        "ja": "実体: {path}",
        "en": "actual executable: {path}",
    },
    "log_open_failed": {
        "ja": (
            "警告: ログを開けませんでした（{log_path}）。出力は捕まえずにそのまま流します。"
            "掲示板に載せた log のパスは生成されません"
        ),
        "en": (
            "Warning: could not open the log ({log_path}). Output is being passed through "
            "uncaptured. No log will be created at the path recorded on the board"
        ),
    },
    "descendants_survived": {
        "ja": (
            "警告: 子プロセスの子孫が残ったまま rb run が終了する。"
            "宣言は解放されるので、掲示板は空・資源は掴まれたままになりうる"
        ),
        "en": (
            "Warning: rb run is exiting while descendants of the child process are still "
            "alive. The declaration will be released, so the board may show free while "
            "the resource is still held"
        ),
    },
    # --- board.py: 監査ログの reason / kind、および内部矛盾を示す例外 -------------------
    #
    # これらは通常 `print` されず監査ログ（audit.jsonl）に残るだけだが、
    # DESIGN.md「Hook Spec」が audit.jsonl を「監査ログから grep 一発で見つかる」
    # 人が読む前提の記録と位置づけているため、CLI の出力と同じ表に載せる。
    "gave_up_deleting": {"ja": "削除を諦めた", "en": "gave up deleting"},
    "gave_up_moving": {"ja": "移動を諦めた", "en": "gave up moving"},
    "gave_up_replacing": {"ja": "置換を諦めた", "en": "gave up replacing"},
    "kind_directory": {"ja": "ディレクトリ", "en": "a directory"},
    "kind_special_file": {"ja": "特殊ファイル", "en": "a special file"},
    "kind_broken_link": {"ja": "壊れたリンク", "en": "a broken link"},
    "reason_required_field_unreadable": {
        "ja": "必須フィールドが読めない",
        "en": "a required field could not be read",
    },
    "reason_nonce_empty": {
        "ja": "nonce が空である",
        "en": "the nonce is empty",
    },
    "reason_nonce_mismatch": {
        "ja": "nonce が一致しない",
        "en": "the nonce does not match",
    },
    "reason_unreadable_after_removal": {
        "ja": "削除直後の再確認で掲示板の一部が読めない",
        "en": "part of the board could not be read on the recheck right after deletion",
    },
    "reason_captured_entry_swapped": {
        "ja": "捕まえた宣言が別物だった",
        "en": "the captured declaration turned out to be a different one",
    },
    "reason_new_declaration_exists": {
        "ja": "新しい宣言が既にある",
        "en": "a new declaration already exists",
    },
    "reason_unkeyed_entry_not_selectable": {
        "ja": "nonce が無い個体は指定できない",
        "en": "an entry without a nonce cannot be targeted individually",
    },
    "cannot_build_selection_from_partial_listing": {
        "ja": (
            "掲示板の一部が読めていない列挙からは、削除できる選択を作れない"
            "（read されなかった側に探している宣言が隠れているかもしれない）"
        ),
        "en": (
            "Cannot select declarations for removal from an incomplete board "
            "listing (the declaration being sought may be in the unread portion)"
        ),
    },
    "remove_confirmed_requires_confirmed_entry": {
        "ja": (
            "remove_confirmed には ConfirmedEntry を渡すこと"
            "（BoardListing.confirmed() または confirm_own_declaration() で作る）"
        ),
        "en": (
            "remove_confirmed must be given a ConfirmedEntry (build one with "
            "BoardListing.confirmed() or confirm_own_declaration())"
        ),
    },
    # --- cli.py: _report_unreadable / _cmd_status ---------------------------------
    "unreadable_files_found": {
        "ja": "読めないファイルが {count} 件あります（どの資源のものか判別できません）",
        "en": "Unreadable files: {count} (cannot determine which resource each file belongs to)",
    },
    "unreadable_files_more": {
        "ja": "ほか {count} 件",
        "en": "and {count} more",
    },
    "unreadable_files_cleanup_hint": {
        "ja": "掃除するなら rb release --clean（資源は指定しない）",
        "en": "To clean them up: rb release --clean (do not specify a resource)",
    },
    "declarations_count_active": {
        "ja": "{count} 件の宣言がある",
        "en": "active declarations: {count}",
    },
    "no_declaration": {
        "ja": "宣言が無い",
        "en": "no declaration",
    },
    "board_unreadable": {
        "ja": "掲示板を読めませんでした。**空とは限りません**",
        "en": "Could not read the board. **This does not mean it is empty.**",
    },
    "board_location": {
        "ja": "掲示板の場所: {root}",
        "en": "Board location: {root}",
    },
    "check_permissions_and_paths": {
        "ja": "権限・パス・ネットワークドライブの接続を確かめること",
        "en": "Check permissions, the path, and the network drive connection",
    },
    "board_empty": {
        "ja": "掲示板は空です（誰も資源を宣言していません）",
        "en": "The board is empty (no one has declared a resource)",
    },
    "check_yourself_before_claim": {
        "ja": "使う前に自分で資源の状態を調べ、rb claim で宣言すること",
        "en": "Before using it, check the resource's state yourself and declare it with rb claim",
    },
    "board_partially_unreadable_notice": {
        "ja": "注意: 掲示板の一部を読めませんでした。**これで全部とは限りません**",
        "en": "Note: part of the board could not be read. **This may not be the full list.**",
    },
    "mark_occupied": {"ja": "使用中", "en": "in use"},
    "mark_free": {"ja": "空き", "en": "free"},
    "marked_as_ghost": {"ja": "※幽霊と判定", "en": "(judged a ghost)"},
    "no_job": {"ja": "(ジョブ未記入)", "en": "(no job noted)"},
    "declaration_label": {"ja": "宣言{index}", "en": "declaration {index}"},
    "elapsed_since": {"ja": "（{duration} 経過）", "en": " ({duration} elapsed)"},
    "eta_at": {"ja": "（{at} 頃）", "en": " (around {at})"},
    "not_a_promise": {
        "ja": "※申告であって約束ではない",
        "en": "(a stated estimate, not a promise)",
    },
    "usage_estimate": {
        "ja": "見積 瞬時最大 {peak} / 平均 {avg}",
        "en": "estimate: peak {peak} / average {avg}",
    },
    "sharing_label": {
        "ja": "共有 {sharing}",
        "en": "handover note: {sharing}",
    },
    "observed_label": {
        "ja": "観測 {note}",
        "en": "observed {note}",
    },
    "observed_at": {
        "ja": "（{at} 時点の申告）",
        "en": "(as reported at {at})",
    },
    "unknown_time": {"ja": "時刻不明", "en": "time unknown"},
    "total_declarations": {
        "ja": "合計   {count} 件の宣言",
        "en": "total   declarations: {count}",
    },
    # --- cli.py: acquire() / _explain_failed_displacement -------------------------
    "lock_not_acquired": {
        "ja": "[rb] 掲示板のロックを取れませんでした（{lock}）。**排他を弱めて続行**します",
        "en": (
            "[rb] Could not acquire the board lock ({lock}). **Continuing with "
            "weaker mutual exclusion.**"
        ),
    },
    "ghost_eviction_skipped_partial": {
        "ja": (
            "[rb] 掲示板の一部を読めませんでした。**退去は行わず**続行します"
            "（生きた宣言を見逃している可能性があります）"
        ),
        "en": (
            "[rb] Part of the board could not be read. **Continuing without "
            "evicting any declarations** (a live declaration may be in the "
            "unread portion)"
        ),
    },
    "reason_board_partially_unreadable": {
        "ja": "掲示板の一部が読めない",
        "en": "part of the board could not be read",
    },
    "reason_forced_takeover": {
        "ja": "強制取得",
        "en": "forced takeover",
    },
    "reason_judged_a_ghost": {
        "ja": "幽霊と判定した",
        "en": "judged a ghost",
    },
    "ghost_reeviction_skipped_partial": {
        "ja": "[rb] 掲示板の一部を読めませんでした。**追加の退去は行わず**続行します",
        "en": (
            "[rb] Part of the board could not be read. **Continuing without any further eviction**"
        ),
    },
    "reason_partial_after_reeviction": {
        "ja": "退去後の再読み取りで一部が読めない",
        "en": "part of the board could not be read on the reread after eviction",
    },
    "resource_in_use_with_count": {
        "ja": "[rb] {label} は使用中です（既に {count} 件の宣言があります）",
        "en": "[rb] {label} is in use (declarations already present: {count})",
    },
    "resource_in_use_self_reported": {
        "ja": "[rb] {label} は使用中です（自分で busy と申告している）",
        "en": "[rb] {label} is in use (you yourself reported it as busy)",
    },
    "holder_line": {
        "ja": "  {session} / {job}（since {since}）",
        "en": "  {session} / {job} (since {since})",
    },
    "handover_note_suffix": {
        "ja": "  申し送り: {sharing}",
        "en": "  handover note: {sharing}",
    },
    "your_report_says_free": {
        "ja": "  あなたの申告は free です。**どちらかが古い。**",
        "en": (
            "  Your report says free, but the board says otherwise. **Either your "
            "report or the board entry is stale.**"
        ),
    },
    "share_or_force_advice": {
        "ja": "  並んで使うなら --share。宣言が古いと判断したなら --force で退けること",
        "en": (
            "  If you intend to share the resource, use --share. If you determine "
            "that a declaration is stale, evict it with --force"
        ),
    },
    "wait_advice_line": {
        "ja": "  空くのを待つなら rb wait {resource_id}",
        "en": "  To wait until it frees up: rb wait {resource_id}",
    },
    "reason_live_declaration_exists": {
        "ja": "生きた宣言がある",
        "en": "a live declaration exists",
    },
    "reason_self_reported_busy": {
        "ja": "自分で busy と申告している",
        "en": "you yourself reported it as busy",
    },
    "declaration_not_saved_notice": {
        "ja": "警告: 宣言を掲示板に残せていません（他セッションからは見えません）",
        "en": (
            "Warning: the declaration was not saved to the board (other sessions cannot see it)"
        ),
    },
    "stray_entries_notice": {
        "ja": "[rb] 壊れたエントリが {count} 件あります（掃除: rb release --clean）",
        "en": "[rb] Corrupt entries: {count} (clean up with rb release --clean)",
    },
    "simultaneous_notice": {
        "ja": ("[rb] **ほぼ同時に {count} 件の宣言が入りました。**先着を決める仕組みはありません"),
        "en": (
            "[rb] **Declarations arrived almost simultaneously: {count}.** "
            "The tool has no mechanism for determining which declaration came first"
        ),
    },
    "overlap_advice": {
        "ja": "  重なって困るなら、どちらかが rb release して rb wait すること",
        "en": "  If the overlap is a problem, have one side run rb release and then rb wait",
    },
    "resource_already_has_declarations": {
        "ja": "[rb] この資源には既に {count} 件の宣言があります",
        "en": "[rb] This resource already has declarations: {count}",
    },
    "sharing_note_suffix": {
        "ja": "共有: {sharing}",
        "en": "handover note: {sharing}",
    },
    "eviction_failed": {
        "ja": "退けようとした宣言を消せませんでした（掲示板に残っています。監査ログを参照）",
        "en": (
            "Could not remove the declaration being evicted (it is still on the "
            "board; see the audit log)"
        ),
    },
    "eviction_unconfirmed": {
        "ja": (
            "退けようとした宣言の消去を確認できませんでした"
            "（掲示板の一部が読めません。監査ログを参照）"
        ),
        "en": (
            "Could not confirm whether the declaration being evicted was removed "
            "(part of the board could not be read; see the audit log)"
        ),
    },
    "eviction_swapped": {
        "ja": "退けようとした宣言が入れ替わりました（他セッションが先に取り直した可能性）",
        "en": (
            "The declaration being evicted was replaced (another session may have "
            "re-claimed it first)"
        ),
    },
    # --- cli.py: _cmd_claim / _warn_not_declared / _cmd_run / _release_after_run --
    "declared_notice": {
        "ja": "宣言しました: {resource} / {job}",
        "en": "Declared: {resource} / {job}",
    },
    "not_declared_warning_line1": {
        "ja": "警告: 宣言を掲示板に残せていません。他セッションからは見えません",
        "en": "Warning: the declaration was not saved to the board. Other sessions cannot see it",
    },
    "not_declared_warning_line2": {
        "ja": "  他セッションはこの利用を知らないまま同じ資源を取りにきます",
        "en": (
            "  Other sessions will try to acquire the same resource without knowing it is in use"
        ),
    },
    "not_declared_warning_line3": {
        "ja": "  作業は止めませんが、衝突を避けたいなら掲示板の状態を確かめること",
        "en": (
            "  The tool will not stop your work, but if you want to avoid a "
            "collision, you must check the board's state"
        ),
    },
    "run_requires_trailing_command": {
        "ja": "実行するコマンドを `--` の後ろに指定してください",
        "en": "Specify the command to run after `--`",
    },
    "run_trailing_example": {
        "ja": '  例: rb run --res GPU0 --job "学習" --observed "..." -- python train.py',
        "en": '  e.g.: rb run --res GPU0 --job "training" --observed "..." -- python train.py',
    },
    "run_not_executed_no_acquisition": {
        "ja": "資源を取得できなかったため、コマンドを実行していません",
        "en": "The command was not run because the resource could not be acquired",
    },
    "run_log_path": {
        "ja": "ログ: {log_path}",
        "en": "log: {log_path}",
    },
    "run_interrupted": {
        "ja": "中断されました",
        "en": "Interrupted",
    },
    "unknown_value": {"ja": "不明", "en": "unknown"},
    "run_exit_reason": {
        "ja": "rb run の終了（exit={code}）",
        "en": "rb run exited (exit={code})",
    },
    "released_notice": {
        "ja": "解放しました: {resource}",
        "en": "Released: {resource}",
    },
    "declaration_swapped": {
        "ja": "宣言が入れ替わりました（解放していません）",
        "en": "The declaration was replaced (nothing was released)",
    },
    "warn_removal_unconfirmed": {
        "ja": (
            "警告: 宣言を取り下げられたか確認できませんでした"
            "（削除直後に掲示板の一部が読めなくなりました）"
        ),
        "en": (
            "Warning: could not confirm whether the declaration was withdrawn "
            "(part of the board became unreadable right after deletion)"
        ),
    },
    "warn_removal_failed": {
        "ja": "警告: 宣言を取り下げられませんでした（掲示板に残っています）",
        "en": "Warning: could not withdraw the declaration (it is still on the board)",
    },
    "declaration_not_withdrawn_absent": {
        "ja": "宣言を取り下げませんでした（既に掲示板にありません）",
        "en": "Did not withdraw the declaration (it was already gone from the board)",
    },
    # --- cli.py: _cmd_wait -----------------------------------------------------------
    "wait_advice": {
        "ja": (
            "  待っている間に自分でも資源の状態を調べること。"
            "空いているのに宣言が残っているなら、\n"
            "  保持者に確認するか、確認が取れなければ人間に相談すること"
            "（本ツールは実測が空きでも宣言を退けない）"
        ),
        "en": (
            "  While waiting, you must also check the resource's state yourself. If "
            "it appears free but the declaration remains,\n"
            "  confirm with the holder; if you cannot confirm with them, consult a "
            "human (this tool does not evict a declaration solely because an "
            "observation reports the resource as free)"
        ),
    },
    "already_released": {
        "ja": "既に解放されています: {resource}",
        "en": "Already released: {resource}",
    },
    "waiting_notice": {
        "ja": "待機します: {resource}",
        "en": "Waiting: {resource}",
    },
    "waiting_notice_with_holder": {
        "ja": "待機します: {resource} <- {session} / {job}",
        "en": "Waiting: {resource} <- {session} / {job}",
    },
    "wait_polling_line": {
        "ja": "{interval} 秒ごとに確認、上限 {timeout} 秒。Ctrl+C で中断できます",
        "en": "checking every {interval}s, up to {timeout}s. Press Ctrl+C to cancel",
    },
    "wait_invalid_duration": {
        "ja": "--interval / --timeout には 0 より大きい有限の秒数を指定してください",
        "en": "--interval / --timeout must be a finite number of seconds greater than 0",
    },
    "wait_interrupted": {
        "ja": "中断しました（宣言はそのままです）",
        "en": "Interrupted (the declaration is left as is)",
    },
    "wait_released": {
        "ja": "全ての宣言が消えました（{polls} 回確認 / {waited} 秒）",
        "en": "All declarations are gone (polls: {polls} / waited: {waited}s)",
    },
    "wait_check_yourself_after_release": {
        "ja": "使う前にもう一度自分で状態を調べること（解放＝空きとは限らない）",
        "en": (
            "Before using it, you must check the resource's state yourself again "
            "(a released declaration does not necessarily mean the resource is free)"
        ),
    },
    "wait_shrank": {
        "ja": "宣言が減りました（残り {holders} 件 / {polls} 回確認 / {waited} 秒）",
        "en": (
            "The number of declarations went down (remaining: {holders} / "
            "polls: {polls} / waited: {waited}s)"
        ),
    },
    "wait_check_yourself_after_shrink": {
        "ja": "入れるかどうかは自分で調べて判断すること。駄目ならもう一度 rb wait すればよい",
        "en": (
            "You must check and decide for yourself whether you can use the "
            "resource. If not, run rb wait again"
        ),
    },
    "wait_broken": {
        "ja": (
            "掲示板を読めないまま上限に達しました（{polls} 回確認 / {waited} 秒）。"
            "使用中かどうかは未確認です"
        ),
        "en": (
            "Reached the limit without ever successfully reading the board "
            "(polls: {polls} / waited: {waited}s). Whether the resource is in use "
            "remains unconfirmed"
        ),
    },
    "wait_timed_out": {
        "ja": "上限に達しました（{polls} 回確認 / {waited} 秒）。まだ使用中です",
        "en": "Reached the limit (polls: {polls} / waited: {waited}s). Still in use",
    },
    "holder_label": {
        "ja": "保持者: {session} / {job}",
        "en": "holder: {session} / {job}",
    },
    "held_since_suffix": {
        "ja": "（{duration} 前から）",
        "en": " (for {duration})",
    },
    # --- cli.py: _cmd_history ---------------------------------------------------------
    "no_past_declarations": {
        "ja": "過去の宣言は見つかりませんでした",
        "en": "No past declarations found",
    },
    "no_release_record": {
        "ja": "解放の記録なし",
        "en": "no release recorded",
    },
    "history_eta_vs_actual_ratio": {
        "ja": "ETA   {eta}  →  実績 {actual}（{ratio:.2f} 倍）",
        "en": "ETA   {eta}  ->  actual {actual} ({ratio:.2f}x)",
    },
    "history_eta_vs_actual": {
        "ja": "ETA   {eta}  →  実績 {actual}",
        "en": "ETA   {eta}  ->  actual {actual}",
    },
    "history_actual_only": {
        "ja": "実績  {actual}",
        "en": "actual  {actual}",
    },
    "history_estimate": {
        "ja": "見積  peak={peak} avg={avg}",
        "en": "estimate  peak={peak} avg={avg}",
    },
    "history_ended": {
        "ja": "終了  {reason}",
        "en": "ended  {reason}",
    },
    "history_footer_advice": {
        "ja": "同じ案件の前回の申告と実績を突き合わせ、次の申告の精度を上げること",
        "en": (
            "Compare the previous declaration for the same work with the actual "
            "outcome, and use that to improve the accuracy of your next declaration"
        ),
    },
    # --- cli.py: _cmd_update / _update_locked ---------------------------------------
    "update_unconfirmed_no_declarations": {
        "ja": "掲示板の一部を読めず、宣言の有無を確認できませんでした（更新は行っていません）",
        "en": (
            "Part of the board could not be read, so whether a declaration exists could "
            "not be confirmed (no update was made)"
        ),
    },
    "no_declaration_found": {
        "ja": "宣言が見つかりませんでした",
        "en": "No declaration found",
    },
    "update_partial_notice": {
        "ja": "注意: 掲示板の一部を読めませんでした（他に自分の宣言があるかもしれません）",
        "en": (
            "Note: part of the board could not be read (other declarations of "
            "yours may be in the unread portion)"
        ),
    },
    "update_multiple_own_declarations": {
        "ja": "自分の宣言が {count} 件あります。最も古いものを書き換えます: {job}",
        "en": "You have {count} declarations of your own. Updating the oldest one: {job}",
    },
    "update_overwriting_foreign": {
        "ja": "警告: 他セッションの宣言を書き換えます: {session} / {job}",
        "en": "Warning: updating another session's declaration: {session} / {job}",
    },
    "update_no_own_declaration": {
        "ja": "自分の宣言はありません（--force で他セッションのものを書き換えられます）",
        "en": (
            "You have no declaration of your own (use --force to update another "
            "session's declaration)"
        ),
    },
    "reason_update_command": {
        "ja": "update コマンド",
        "en": "update command",
    },
    "update_conflict": {
        "ja": (
            "更新をやめました: 読んでから書くまでに宣言が入れ替わりました"
            "（他セッションが取り直した可能性）"
        ),
        "en": (
            "Update aborted: the declaration changed between reading and writing "
            "(another session may have re-claimed it)"
        ),
    },
    "update_failed": {
        "ja": "更新できませんでした（掲示板に書けません。監査ログを参照）",
        "en": "Could not update (cannot write to the board; see the audit log)",
    },
    "updated_notice": {
        "ja": "更新しました: {resource} / {job}",
        "en": "Updated: {resource} / {job}",
    },
    # --- cli.py: _cmd_release / _release_by_nonce -----------------------------------
    "reason_release_clean": {"ja": "release --clean", "en": "release --clean"},
    "unreadable_files_removed": {
        "ja": "読めないファイルを {count} 件消しました",
        "en": "Unreadable files removed: {count}",
    },
    "no_unreadable_files": {
        "ja": "読めないファイルはありませんでした",
        "en": "There were no unreadable files",
    },
    "board_scan_incomplete": {
        "ja": "掲示板を完全に走査できませんでした（他に読めないファイルがあるかもしれません）",
        "en": "Could not fully scan the board (there may be other unreadable files)",
    },
    "warn_could_not_remove_count": {
        "ja": "警告: {count} 件を消せませんでした（他プロセスが読んでいる可能性）",
        "en": (
            "Warning: unreadable files not removed: {count} (another process may be reading them)"
        ),
    },
    "release_requires_resource_or_flag": {
        "ja": "資源 ID を指定するか、--clean か --nonce を付けてください",
        "en": "Specify a resource ID, or add --clean or --nonce",
    },
    "release_nonce_partial_unreadable": {
        "ja": (
            "掲示板の一部を読めませんでした。解放は未確認です"
            "（読めなかった側に一致する宣言が隠れているかもしれません）"
        ),
        "en": (
            "Part of the board could not be read. The release is unconfirmed "
            "(a matching declaration may be in the unread portion)"
        ),
    },
    "nonce_no_match": {
        "ja": "nonce '{prefix}' に一致する宣言が見つかりませんでした",
        "en": "No declaration matches nonce '{prefix}'",
    },
    "reason_no_matching_declaration": {
        "ja": "一致する宣言が無い",
        "en": "no matching declaration",
    },
    "nonce_ambiguous_matches": {
        "ja": "nonce '{prefix}' が {count} 件に一致します。もっと長い桁数を指定してください",
        "en": "nonce '{prefix}' matches {count} declarations. Specify a longer prefix",
    },
    "nonce_match_line": {
        "ja": "  nonce {nonce}  {resource}  {session} / {job}（since {since}）",
        "en": "  nonce {nonce}  {resource}  {session} / {job} (since {since})",
    },
    "reason_ambiguous_prefix_match": {
        "ja": "前方一致が曖昧",
        "en": "ambiguous prefix match",
    },
    "nonce_not_own": {
        "ja": "nonce '{prefix}' は自分の宣言ではありません（{session} / {job}）",
        "en": "nonce '{prefix}' is not your own declaration ({session} / {job})",
    },
    "nonce_force_hint": {
        "ja": "  他セッションの宣言を消すなら rb release --nonce {prefix} --force",
        "en": "  To remove another session's declaration: rb release --nonce {prefix} --force",
    },
    "reason_not_own_declaration": {
        "ja": "自分の宣言ではない",
        "en": "not your own declaration",
    },
    "resource_nonce_mismatch": {
        "ja": (
            "食い違いがあります: 指定した資源は {wanted} ですが、"
            "--nonce が指しているのは {actual} です"
        ),
        "en": ("Mismatch: the resource you specified is {wanted}, but --nonce points to {actual}"),
    },
    "nonce_entry_line": {
        "ja": "  nonce {nonce}  {session} / {job}（since {since}）",
        "en": "  nonce {nonce}  {session} / {job} (since {since})",
    },
    "reason_resource_nonce_mismatch": {
        "ja": "resource と --nonce が食い違う",
        "en": "resource and --nonce disagree",
    },
    "reason_release_nonce_force_command": {
        "ja": "release --nonce --force コマンド",
        "en": "release --nonce --force command",
    },
    "force_released_with_nonce": {
        "ja": "強制解放しました: {resource}（nonce {nonce}, {session} / {job}）",
        "en": "Forcibly released: {resource} (nonce {nonce}, {session} / {job})",
    },
    "declaration_already_absent_nonce": {
        "ja": "宣言は既にありませんでした: nonce {nonce}",
        "en": "The declaration was already gone: nonce {nonce}",
    },
    "reason_release_nonce_command": {
        "ja": "release --nonce コマンド",
        "en": "release --nonce command",
    },
    "released_with_nonce": {
        "ja": "解放しました: {resource}（nonce {nonce}）",
        "en": "Released: {resource} (nonce {nonce})",
    },
    "declaration_not_withdrawn_maybe_absent_nonce": {
        "ja": "宣言を取り下げませんでした（既に掲示板に無い可能性）: nonce {nonce}",
        "en": (
            "Did not withdraw the declaration (it may already be gone from the "
            "board): nonce {nonce}"
        ),
    },
    # --- cli.py: _release_forced / _release_own -------------------------------------
    "force_release_partial_unreadable": {
        "ja": (
            "掲示板の一部を読めませんでした。強制解放は未確認です"
            "（読めなかった側にこの資源の宣言が隠れているかもしれません）"
        ),
        "en": (
            "Part of the board could not be read. The forced release is unconfirmed "
            "(a declaration for this resource may be in the unread portion)"
        ),
    },
    "reason_release_forced_command": {
        "ja": "release コマンド（強制）",
        "en": "release command (forced)",
    },
    "no_declarations_for_resource": {
        "ja": "宣言はありませんでした: {resource}",
        "en": "There were no declarations: {resource}",
    },
    "force_released_notice": {
        "ja": "強制解放しました: {resource}（{count} 件）",
        "en": "Forcibly released: {resource} (declarations: {count})",
    },
    "warn_could_not_confirm_removal_count": {
        "ja": (
            "警告: {count} 件は消せたか確認できませんでした"
            "（削除直後に掲示板の一部が読めなくなりました）"
        ),
        "en": (
            "Warning: removals not confirmed: {count} (part of the board became "
            "unreadable right after deletion)"
        ),
    },
    "warn_swapped_not_removed_count": {
        "ja": "警告: {count} 件は他セッションが取り直していたため消していません",
        "en": (
            "Warning: replacement declarations left untouched: {count} (other "
            "sessions had already reclaimed the resource)"
        ),
    },
    "release_own_partial_unreadable": {
        "ja": (
            "掲示板の一部を読めませんでした。解放は未確認です"
            "（読めなかった側に自分の宣言が隠れているかもしれません）"
        ),
        "en": (
            "Part of the board could not be read. The release is unconfirmed "
            "(one of your own declarations may be in the unread portion)"
        ),
    },
    "release_own_ambiguous": {
        "ja": "自分の宣言が {count} 件あります。曖昧なので何も消しません",
        "en": (
            "You have {count} declarations of your own. The target is ambiguous, "
            "so nothing will be removed"
        ),
    },
    "nonce_job_since_line": {
        "ja": "  nonce {nonce}  {job}（since {since}）",
        "en": "  nonce {nonce}  {job} (since {since})",
    },
    "release_nonce_single_hint": {
        "ja": "  1 本だけ消すには rb release --nonce <nonce の先頭 8 桁>",
        "en": "  To remove just one: rb release --nonce <first 8 characters of the nonce>",
    },
    "release_all_hint": {
        "ja": "  まとめて消すには rb release {resource} --all",
        "en": "  To remove them all at once: rb release {resource} --all",
    },
    "no_own_declaration_for_resource": {
        "ja": "自分の宣言はありません: {resource}",
        "en": "You have no declaration of your own: {resource}",
    },
    "release_force_hint": {
        "ja": "  他セッションの宣言を消すなら rb release {resource} --force",
        "en": "  To remove another session's declaration: rb release {resource} --force",
    },
    "reason_release_command": {
        "ja": "release コマンド",
        "en": "release command",
    },
    "released_notice_count": {
        "ja": "解放しました: {resource}（{count} 件）",
        "en": "Released: {resource} (declarations: {count})",
    },
    "current_holder_line": {
        "ja": "  現在: {session} / {job}（since {since}）",
        "en": "  currently: {session} / {job} (since {since})",
    },
    # --- cli.py: _add_declaration_options / build_parser (argparse help/description) --
    "help_job": {"ja": "何をするか（1 行）", "en": "What you are doing (one line)"},
    "help_observed": {
        "ja": "自分で調べて何を見たか（例: 'nvidia-smi: compute apps なし'）",
        "en": "What you observed yourself (e.g. 'nvidia-smi: no compute apps')",
    },
    "help_eta": {
        "ja": (
            "終わるまでの見込み。'30m' '2h' '1h30m' なら絶対時刻を機械が計算して併記する。"
            "自由記述も可（'モデル次第' 等）。**判断には使わない**"
        ),
        "en": (
            "Expected time to completion. For '30m', '2h', or '1h30m', the tool "
            "calculates and displays an absolute time alongside it. Free-form text "
            "is also accepted (e.g. 'depends on the model'). **Not used for any "
            "decision**"
        ),
    },
    "help_found": {
        "ja": "調べた結論。既定は unknown（分からなかった）",
        "en": "What you concluded. Defaults to unknown (could not tell)",
    },
    "help_peak": {
        "ja": "利用見積もりの瞬時最大（例: 'VRAM 6GB' '80%%' '4 cores'）",
        "en": "Estimated peak usage (e.g. 'VRAM 6GB', '80%%', '4 cores')",
    },
    "help_avg": {
        "ja": "利用見積もりの平均（同上）",
        "en": "Estimated average usage (same format as above)",
    },
    "help_sharing": {
        "ja": (
            "次に来る人への申し送り（例: 'VRAM 残 6GB まで空き' '15 コア占有'）。"
            "**許可を与える旗ではない**（与える保持者がいない）。本ツールは解釈しない"
        ),
        "en": (
            "A handover note for whoever comes next (e.g. '6 GB of VRAM still "
            "available', '15 cores in use'). **This is not a flag that grants "
            "permission** (there is no holder who can grant it). This tool does not "
            "interpret its contents"
        ),
    },
    "help_log": {"ja": "進捗が読めるログのパス", "en": "Path to a log where progress can be read"},
    "help_share": {
        "ja": "既に宣言がある資源へ並んで使う（誰の宣言も消さない）",
        "en": (
            "Use a resource that already has a declaration, alongside it "
            "(removes no one's declaration)"
        ),
    },
    "help_force_claim": {
        "ja": "他者の宣言を退けて強制的に取得する",
        "en": "Evict another session's declaration and acquire it by force",
    },
    "parser_description": {
        "ja": "並行する Claude Code セッション間で有限資源の使用状況を共有する掲示板",
        "en": (
            "A board for sharing the usage status of finite resources among "
            "concurrent Claude Code sessions"
        ),
    },
    "parser_epilog": {
        "ja": (
            "本ツールは資源を調べない。調べるのは資源を使おうとするセッションの仕事であり、"
            "claim はその結果の申告を必須とする。"
        ),
        "en": (
            "This tool does not check resources itself. Checking is the job of the "
            "session that wants to use one, and claim requires reporting the result "
            "of that check."
        ),
    },
    "help_home": {
        "ja": "掲示板のルート（既定は環境依存）",
        "en": "The board's root directory (default depends on the environment)",
    },
    "help_version": {
        "ja": "版と実行元のパッケージディレクトリを表示して終了する",
        "en": "Print the version and the package directory it runs from, then exit",
    },
    "help_status": {
        "ja": "資源の状態を表示する（常に全件）",
        "en": "Show the state of resources (always all of them)",
    },
    "description_status": {
        "ja": (
            "宣言のある全資源を表示する。資源 ID は受け取らない——名指しで絞ると、"
            "表記の揺れ（大文字小文字は別資源）で相手の宣言が見えず「空き」と誤って"
            "答えることがあるため（issue #9）。"
        ),
        "en": (
            "Shows every resource that has a declaration. It does not take a "
            "resource ID — filtering by name can miss another session's "
            "declaration because of spelling variation (different letter case "
            "means a different resource) and incorrectly report the resource as "
            "free (issue #9)."
        ),
    },
    "help_json": {"ja": "JSON で出力する", "en": "Output as JSON"},
    "help_claim": {
        "ja": "資源を宣言する（先に自分で調べること）",
        "en": "Declare a resource (check it yourself first)",
    },
    "description_claim": {
        "ja": (
            "資源を宣言する。--observed には「自分が何を見たか」を書く。"
            "本ツールは中身を解釈せず、観測点として掲示板に残すだけである。"
        ),
        "en": (
            "Declares a resource. Write what you yourself observed in --observed. "
            "This tool does not interpret the content — it only records it on the "
            "board as an observation data point."
        ),
    },
    "help_resource_id": {"ja": "資源 ID", "en": "Resource ID"},
    "help_release": {
        "ja": "宣言を解放する（自分のものだけ）",
        "en": "Release a declaration (your own only)",
    },
    "description_release": {
        "ja": (
            "自分の宣言を取り下げる。自分の宣言が 2 件以上あるときは、曖昧なので"
            "既定では何も消さずに拒否する（--all でまとめて消せる）。"
            "--nonce を使えば資源 ID 無しで 1 本だけを狙って消せる。"
            "他セッションの宣言まで消すのは --force だけである。"
        ),
        "en": (
            "Withdraws your own declaration. If you have two or more declarations "
            "of your own, the target is ambiguous, so by default the command "
            "refuses to remove anything (--all removes them all). With --nonce, "
            "you can target and remove exactly one declaration without a resource "
            "ID. Only --force removes declarations from other sessions."
        ),
    },
    "help_resource_id_optional": {
        "ja": "資源 ID（--clean / --nonce のときは不要）",
        "en": "Resource ID (not needed with --clean / --nonce)",
    },
    "help_nonce": {
        "ja": (
            "資源 ID の代わりに nonce の前方一致で 1 本を指定する（rb status に表示される"
            "先頭 8 桁でよい）。既定では自分が所有する宣言だけに絞り込み、一意に決まらなければ"
            "何も消さず候補を挙げて拒否する。他セッションの宣言を消すには --force を併用する"
        ),
        "en": (
            "Target exactly one declaration by matching a nonce prefix, instead of "
            "a resource ID (the first 8 characters shown in rb status are enough). "
            "By default, matches are limited to your own declarations. If the "
            "prefix does not identify exactly one declaration, nothing is removed "
            "and the candidates are listed. Combine with --force to remove another "
            "session's declaration"
        ),
    },
    "help_all": {
        "ja": "自分の宣言が複数あっても全部まとめて解放する（曖昧さの拒否を明示的に上書きする）",
        "en": (
            "Release all of your own declarations even when there are several "
            "(explicitly overrides the default refusal when the target is ambiguous)"
        ),
    },
    "help_force_release": {
        "ja": "他セッションの宣言も強制的に解放する",
        "en": "Also forcibly release another session's declaration",
    },
    "help_clean": {
        "ja": "読めないファイルを消す（どの資源のものか判別できないので資源は指定しない）",
        "en": (
            "Remove unreadable files (do not specify a resource, because the "
            "resource for each file cannot be identified)"
        ),
    },
    "help_run": {
        "ja": "資源を宣言してコマンドを実行し、終了時に必ず解放する",
        "en": "Declare a resource, run a command, and always release the declaration on exit",
    },
    "description_run": {
        "ja": (
            "宣言・ログ出力・解放を機械的に行う。解放は finally で行うため、"
            "異常終了でも中断でもエントリは残らない。"
            "終了コードは子プロセスのものをそのまま返す。"
        ),
        "en": (
            "Mechanically handles declaring, logging output, and releasing. Release "
            "runs in a finally block, so the entry is never left behind whether the "
            "command fails or is interrupted. The exit code is passed through from "
            "the child process."
        ),
    },
    "help_update": {
        "ja": "自分の宣言を書き換える（見積もりや ETA を実態に合わせる）",
        "en": "Update your own declaration (bring the estimate or ETA in line with reality)",
    },
    "description_update": {
        "ja": (
            "既に出している宣言の申告値を更新する。ジョブが進んで使用量が変わったときに、"
            "掲示板を実態へ寄せるために使う。"
        ),
        "en": (
            "Updates the reported values of a declaration you have already made. Use "
            "this to bring the board in line with reality once the job has "
            "progressed and usage has changed."
        ),
    },
    "help_eta_update": {"ja": "終わるまでの見込み", "en": "How long you expect this to take"},
    "help_peak_update": {"ja": "利用見積もりの瞬時最大", "en": "Estimated peak usage"},
    "help_avg_update": {"ja": "利用見積もりの平均", "en": "Estimated average usage"},
    "help_sharing_update": {
        "ja": "次に来る人への申し送り",
        "en": "A handover note for whoever comes next",
    },
    "help_force_update": {
        "ja": "他者の宣言でも書き換える",
        "en": "Update another session's declaration as well",
    },
    "help_wait": {
        "ja": "資源を宣言している者が減るまで待つ",
        "en": "Wait until fewer sessions declare this resource",
    },
    "description_wait": {
        "ja": (
            "宣言の数が減る（誰かが解放する）まで待つ。相乗りが**増えた**ときには起きない"
            "（資源はさらに詰まっているため）。ETA では打ち切らない（申告であって約束ではない）。"
            "打ち切るのは --timeout だけである。毎回のポーリングは監査ログに残る。"
        ),
        "en": (
            "Waits until the number of declarations drops (someone releases). It "
            "does not return when the number of sharers **increases** (the resource "
            "is even more contended then). It does not stop at the ETA (a stated "
            "estimate, not a promise) — only --timeout stops it. Every poll is "
            "recorded in the audit log."
        ),
    },
    "help_interval": {
        "ja": "ポーリング間隔の秒数（既定 {default}）",
        "en": "Polling interval in seconds (default {default})",
    },
    "help_timeout": {
        "ja": "待機の上限秒数（既定 {default}）。超えたら一度戻る",
        "en": (
            "Maximum wait time in seconds (default {default}). Returns when the limit is exceeded"
        ),
    },
    "help_history": {
        "ja": "過去の宣言を振り返る（見積もりの根拠にする）",
        "en": "Review past declarations (as grounds for your estimates)",
    },
    "description_history": {
        "ja": (
            "監査ログから過去の宣言と解放を拾う。前回どう見積もって実際どうだったかを"
            "見返すためのもので、見積もりの精度を回ごとに上げるために使う。"
        ),
        "en": (
            "Pulls past declarations and releases from the audit log. It is meant "
            "for looking back at what you estimated last time versus what actually "
            "happened, so you can sharpen your estimate each time."
        ),
    },
    "help_resource_id_history": {
        "ja": "資源 ID（省略時は全件）",
        "en": "Resource ID (all resources if omitted)",
    },
    "help_limit": {
        "ja": "表示する件数（既定 20）",
        "en": "Number of entries to show (default 20)",
    },
    # --- cli.py: main() の catch-all -------------------------------------------------
    "main_interrupted": {
        "ja": "中断しました",
        "en": "Interrupted",
    },
    "main_internal_error": {
        "ja": "[resource-broker] 内部エラーのため判定を省略します: {error}",
        "en": "[resource-broker] Skipping the judgment due to an internal error: {error}",
    },
    "nonce_empty_prefix": {
        "ja": "nonce が空です。前方一致させる値を指定してください",
        "en": "The nonce prefix is empty. Specify a prefix to match",
    },
}


def tr(key: str, **kwargs: object) -> str:
    """メッセージ表から 1 件取り出し、書式指定子を埋めて返す。

    **``en`` が欠けていれば ``ja`` を返す。** 訳が追いつかなくても日本語で
    必ず動く、という設計上の約束をここで実装として保証する。
    """
    table = MESSAGES[key]
    template = table.get(current_language()) or table["ja"]
    return template.format(**kwargs) if kwargs else template
