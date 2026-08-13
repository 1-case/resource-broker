"""資源 ID の正規化とファイル名変換の性質を検証する。"""

from __future__ import annotations

import re

import pytest

from resource_broker import naming

SAFE = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# 実際に扱う資源 ID。ファイル名に使えない文字を含むものを揃える。
REAL_IDS = [
    "COM3",
    "GPU-0f8f1c2d-1111-2222-3333-444455556666",
    r"USB\VID_0403&PID_6001\A50285BI",
    r"\\nas\share",
    "api.openai.com/v1/embeddings",
    "tcp:7899",
    "C:",
    "ram",
]

# Windows の予約名。ハッシュ接尾辞によりそのままの名前にならないことを確認する。
RESERVED = ["nul", "con", "prn", "aux", "com1", "lpt9", "NUL", "CON"]


@pytest.mark.parametrize("resource_id", REAL_IDS + RESERVED)
def test_safe_filename_uses_only_portable_characters(resource_id: str) -> None:
    """どんな資源 ID でも、ファイル名は移植可能な文字だけになる。"""
    assert SAFE.match(naming.safe_filename(resource_id))


@pytest.mark.parametrize("resource_id", RESERVED)
def test_safe_filename_never_collides_with_windows_reserved_names(resource_id: str) -> None:
    """予約名がそのままファイル名にならない（拡張子を除いた部分で判定される）。"""
    stem = naming.safe_filename(resource_id).split(".", 1)[0]
    assert stem.lower() not in {r.lower() for r in RESERVED}


def test_safe_filename_is_deterministic() -> None:
    """同じ ID からは常に同じファイル名が得られる。"""
    assert naming.safe_filename(REAL_IDS[0]) == naming.safe_filename(REAL_IDS[0])


def test_safe_filename_separates_ids_that_sanitize_alike() -> None:
    """安全化すると同じ綴りになる ID でも、ハッシュにより区別される。"""
    left = naming.safe_filename(r"USB\VID_1")
    right = naming.safe_filename("USB/VID_1")
    assert left != right


def test_normalize_adds_hostname_prefix() -> None:
    """プレフィックスが無い ID にはホスト名が付く。"""
    assert naming.normalize("COM3", host="pc-a") == "pc-a::COM3"


def test_normalize_keeps_existing_prefix() -> None:
    """既にプレフィックスが付いていれば二重に付けない。"""
    assert naming.normalize("pc-a::COM3", host="pc-b") == "pc-a::COM3"


@pytest.mark.parametrize(
    "resource_id",
    ["api.openai.com/v1/embeddings", "//nas/share", r"\\nas\share", r"USB\VID_0403&PID_6001\A5"],
)
def test_normalize_prefixes_ids_that_contain_separators(resource_id: str) -> None:
    """``/`` や ``\\`` を含む ID もプレフィックスの対象になる。

    区切りに ``/`` を使うと ``api.openai.com/v1/embeddings`` の先頭セグメントを
    ホスト名と誤認し、プレフィックスが付かないまま通ってしまう。
    """
    assert naming.normalize(resource_id, host="pc-a") == f"pc-a::{resource_id}"


@pytest.mark.parametrize("resource_id", REAL_IDS)
def test_display_default_strips_host_prefix(resource_id: str) -> None:
    """表示名にはホスト名プレフィックスが含まれない。"""
    normalized = naming.normalize(resource_id, host="pc-a")
    assert naming.display_default(normalized) == resource_id


def test_normalize_rejects_empty_id() -> None:
    """空の ID は受け付けない。"""
    with pytest.raises(ValueError, match="資源 ID"):
        naming.normalize("   ")


# --- 見出しは資源 ID を隠さない ---------------------------------------------------


def test_label_keeps_the_resource_id_when_a_display_name_is_given() -> None:
    """``--display`` は資源 ID を**置き換えない**。

    display は「UUID を読みやすくするための資源の別名」であって、資源の同一性を
    置き換えるものではない。実運用で display にジョブ名（``malm E017 学習``）が
    入り、GPU0 が押さえられていることが一覧とフックの通知から消えた。
    """
    resource_id = naming.normalize("GPU0")

    assert naming.label(resource_id, "malm E017 学習") == "GPU0（malm E017 学習）"


def test_label_falls_back_to_the_resource_id() -> None:
    """表示名が無ければ資源 ID だけを出す。"""
    resource_id = naming.normalize("COM5")

    assert naming.label(resource_id, "") == "COM5"
    assert naming.label(resource_id, None) == "COM5"


def test_label_does_not_repeat_a_redundant_display_name() -> None:
    """表示名が資源 ID と同じなら括弧を付けない（既定はこの形になる）。"""
    resource_id = naming.normalize("GPU0")

    assert naming.label(resource_id, "GPU0") == "GPU0"
    assert naming.label(resource_id, resource_id) == "GPU0"


def test_label_ignores_a_whitespace_only_display_name() -> None:
    """空白だけの表示名で括弧を作らない。"""
    resource_id = naming.normalize("GPU0")

    assert naming.label(resource_id, "   ") == "GPU0"
