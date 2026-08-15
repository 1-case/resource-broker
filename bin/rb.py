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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resource_broker.cli import main  # noqa: E402 - sys.path を整えた後でなければ import できない

if __name__ == "__main__":
    raise SystemExit(main())
