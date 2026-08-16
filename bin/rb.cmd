@echo off
rem resource-broker の CLI を、インストール無しで起動する（cmd.exe 用）。詳細は bin/rb を参照。
rem
rem **遅延展開が要る。** cmd は for の本体をまとめて解析するため、%ERRORLEVEL% は
rem ループに入る前の値（通常 0）に固定される。それでは python の失敗を成功として返す。
rem 実測で確認済み: 子が 7 で終了しても exit /b %ERRORLEVEL% は 0 を返した。
rem 「rb run は走らなかったジョブを成功と報告しない」という唯一の非対称な契約が、
rem cmd 経路でだけ裏返っていた。
setlocal enabledelayedexpansion
for %%P in (python3.13.exe python3.12.exe python3.11.exe py.exe python3.exe python.exe) do (
    where %%P >nul 2>&1 && (
        rem Microsoft Store の App Execution Alias は 0 バイトの実体で where に当たるが、
        rem 起動すると Store が開いて 9009 で終わる。一度空実行して本物か確かめる。
        %%P -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && (
            %%P "%~dp0rb.py" %*
            exit /b !ERRORLEVEL!
        )
    )
)
echo rb: python が見つかりません（resource-broker は Python 3.11 以上を必要とします） 1>&2
exit /b 127
