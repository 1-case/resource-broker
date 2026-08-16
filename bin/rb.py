"""``rb`` をインストール無しで起動する土台。

**依存パッケージがゼロ（stdlib のみ）だから成立する。** ``uv tool install`` を経ずに、
リポジトリ（＝プラグインの展開先）の ``src`` を import 経路へ足して CLI を呼ぶだけでよい。

``PYTHONPATH`` を使わないのは、区切り文字が OS で違うためである（POSIX は ``:``、
Windows は ``;``）。Git Bash から Windows の Python を呼ぶ組み合わせでは、シェルと
インタプリタで期待が食い違う。ここで ``sys.path`` を直接触れば、その問題が消える。
"""

from __future__ import annotations

import sys
from pathlib import Path

# **import の前に版を確かめる。** 下限は StrEnum が入った 3.11 である。それより古い
# python（macOS の /usr/bin/python3 は 3.9、Ubuntu 22.04 は 3.10）でそのまま import すると
# `from enum import StrEnum` が ImportError になり、利用者が受け取るのは内部を指す生の
# traceback である。原因（python が古い）にたどり着けない。
if sys.version_info < (3, 11):
    sys.stderr.write(
        f"rb: Python 3.11 以上が要ります"
        f"（見つかったのは {sys.version.split()[0]}: {sys.executable}）\n"
    )
    raise SystemExit(127)

# **自分の出力だけを UTF-8 にする。** Windows の Python はコンソールのコードページ
# （cp932 等）で書くが、Git Bash の端末は UTF-8 なので日本語が化ける（実測）。
#
# **環境変数（PYTHONUTF8）で解決してはならない。** `rb run -- python train.py` の子が
# それを継承し、`open()` の既定エンコーディングが cp932 から UTF-8 に変わる。
# それまで動いていたジョブが UnicodeDecodeError で落ちうるし、「Git Bash から
# rb run した時だけ落ちる」ので原因にたどり着けない。**利用者のジョブの挙動を
# 変えることは目的ではない。**
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 直せなくても実行は続ける
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resource_broker.cli import main  # noqa: E402 - sys.path を整えた後でなければ import できない

if __name__ == "__main__":
    _code = main()
    # **終了前に自分で flush する。** 小さい出力は print の時点では例外にならず、
    # インタプリタ終了時の flush で失敗する。CPython は最終化が失敗すると
    # **終了コードを 120 に差し替える**ため、`rb run ... | head` が子の終了コードを
    # 返さなくなる（「走らなかったジョブを成功と報告しない」が別の形で破れる）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.flush()
        except Exception:  # noqa: BLE001 - 出せなかったことで終了コードを変えない
            try:
                _stream.close()
            except Exception:  # noqa: BLE001
                pass
    raise SystemExit(_code)
