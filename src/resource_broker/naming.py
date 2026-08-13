r"""資源 ID の正規化と、ファイル名への安全な変換。

命名の原則は 2 つ。**その端末で一意**であること、**人間が現物を指させる**こと。
両方を満たすのは Windows のデバイスマネージャの表記であり、正規名はそれに合わせる
（DESIGN.md「Resource Naming」）。

資源 ID には ``\\nas\share`` や ``USB\VID_0403&PID_6001\A50285BI`` のように
ファイル名に使えない文字が含まれる。掲示板のファイル名は安全化した文字列に
短縮ハッシュを付けたものとし、**真の ID はファイルの中に持つ**。
"""

from __future__ import annotations

import hashlib
import re

from . import platform_info

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_BASE = 64

#: ホスト名と資源 ID の区切り。
#:
#: ``/`` は使えない。UNC パス（``\\nas\share``）や Web API の ID
#: （``api.openai.com/v1/embeddings``）が ``/`` を含むため、先頭のセグメントが
#: ホスト名なのか資源 ID の一部なのかを機械的に判別できなくなる。
#: ``::`` は Windows のパスにも URL のホスト部にも現れないため区切りとして安全である。
HOST_SEP = "::"


def normalize(resource_id: str, host: str | None = None) -> str:
    """資源 ID に ``<hostname>::`` のプレフィックスを付けて正規化する。

    現在は 1 台しか扱わないが、将来マシンが増えたときに ID の意味が変わらないよう、
    最初からプレフィックスを確保しておく。

    Parameters
    ----------
    resource_id : str
        資源 ID。既に ``::`` を含んでいればプレフィックス済みとみなしてそのまま返す。
    host : str, optional
        ホスト名。省略時はこのマシンのホスト名を使う。

    Returns
    -------
    str
        正規化済みの資源 ID。

    Notes
    -----
    Web API のレート制限のように**マシンに紐づかない資源**は、本来ホスト名を
    付けるべきではない。Phase 1 はマシンローカルの資源だけを扱うため一律に付ける。
    グローバルスコープの導入は Phase 5 の課題である（DESIGN.md「Open Issues」）。
    """
    trimmed = (resource_id or "").strip()
    if not trimmed:
        raise ValueError("資源 ID が空である")
    if HOST_SEP in trimmed:
        return trimmed
    return f"{host or platform_info.hostname()}{HOST_SEP}{trimmed}"


def safe_filename(resource_id: str) -> str:
    """資源 ID を掲示板のファイル名（拡張子なし）に変換する。

    衝突を避けるため ID の SHA-256 の先頭 8 桁を必ず付ける。
    ハッシュが付くことで ``nul`` や ``con`` などの Windows 予約名にもならない
    （CLAUDE.md「File Safety」）。

    Parameters
    ----------
    resource_id : str
        資源 ID（正規化済みでもそうでなくてもよい）。

    Returns
    -------
    str
        ``[A-Za-z0-9._-]`` のみからなるファイル名。
    """
    digest = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:8]
    base = _UNSAFE.sub("_", resource_id).strip("._-")[:_MAX_BASE]
    if not base:
        base = "resource"
    return f"{base}-{digest}"


def display_default(resource_id: str) -> str:
    """表示名が与えられていないときの既定表示名を返す（ホスト名プレフィックスを外す）。"""
    return resource_id.split(HOST_SEP, 1)[-1]


def label(resource_id: str, display: str | None = None) -> str:
    """掲示板の一覧に出す見出し。**資源 ID を必ず含める。**

    ``--display`` は「UUID を読みやすくするための資源の別名」であって
    （DESIGN.md の例: ``GPU0 / RTX 4060 Laptop 8GB``）、資源の同一性を
    置き換えるものではない。置き換えを許すと、掲示板を読んだ側が
    **その宣言がどの資源を押さえているのか判断できなくなる。**

    実運用で ``display`` にジョブ名（``malm E017 学習``）が入り、一覧と
    フックの通知から ``GPU0`` の文字が消えた。取得の排他は資源 ID で効いていたため
    衝突は起きなかったが、GPU を使おうとする側は「見覚えのないジョブ名」しか
    見えず、空きだと誤読しうる状態だった。掲示板は読まれて初めて意味を持つので、
    読める形を保つことは排他が効いていることと同じだけ重要である。

    Parameters
    ----------
    resource_id : str
        正規化済みの資源 ID。
    display : str, optional
        申告された表示名。空・既定値と同じ・資源 ID そのものなら添えない。

    Returns
    -------
    str
        ``GPU0`` または ``GPU0（malm E017 学習）``。
    """
    base = display_default(resource_id)
    text = (display or "").strip()
    if not text or text == base or text == resource_id:
        return base
    return f"{base}（{text}）"
