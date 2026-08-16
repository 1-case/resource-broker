@echo off
rem resource-broker の CLI を、インストール無しで起動する（cmd.exe 用）。詳細は bin/rb を参照。
rem
rem **enabledelayedexpansion を使わない。** 使うと %* の展開結果が `!...!` として再走査され、
rem 引数に含まれる `!` が変数展開されて**痕跡なく消える**。--observed は本ツール唯一の
rem 強制点であり、そこが黙って壊れる（`--observed "0MiB! まだ空き!"` の中身が失われる）。
rem 終了コードは、for を抜けてから素の %ERRORLEVEL% を返せば正しく取れる。
rem （for の本体内では %ERRORLEVEL% がブロック解析時に固定される。実測で確認済み。）
setlocal
set "RBPY="
for %%P in (py.exe python3.13.exe python3.12.exe python3.11.exe python3.exe python.exe) do (
    if not defined RBPY (
        rem Microsoft Store の App Execution Alias は 0 バイトの実体で where に当たるが、
        rem 起動すると Store が開いて 9009 で終わる。版を聞いて本物かどうか確かめる。
        where %%P >nul 2>&1 && %%P -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "RBPY=%%P"
    )
)
if not defined RBPY (
    echo rb: Python 3.11 以上が見つかりません 1>&2
    exit /b 127
)
%RBPY% "%~dp0rb.py" %*
exit /b %ERRORLEVEL%
