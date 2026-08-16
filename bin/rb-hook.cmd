@echo off
rem フックを、python の在り処を気にせず起動する（cmd.exe 用）。詳細は bin/rb-hook を参照。
rem 必ず 0 で戻る（フックはユーザーの作業を止めてはならない）。
rem 遅延展開を使わない理由は bin/rb.cmd と同じ（引数の `!` が消える）。
setlocal
if "%~1"=="" exit /b 0
if not exist "%~dp0..\hooks\%~1.py" exit /b 0
set "RBPY="
for %%P in (py.exe python3.exe python.exe) do (
    if not defined RBPY (
        rem サイズ 0 の Store スタブを、追加のプロセス起動なしで弾く。
        for %%F in ("%%~$PATH:P") do if not "%%~zF"=="" if %%~zF gtr 0 set "RBPY=%%P"
    )
)
if not defined RBPY exit /b 0
%RBPY% "%~dp0..\hooks\%~1.py"
exit /b 0
