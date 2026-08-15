@echo off
rem resource-broker の CLI を、インストール無しで起動する（cmd.exe 用）。
rem 詳細は bin/rb を参照。python が無いときに 0 を返さないのも同じ理由である。
setlocal
for %%P in (python.exe py.exe) do (
    where %%P >nul 2>&1 && (
        "%%~nP" "%~dp0rb.py" %*
        exit /b %ERRORLEVEL%
    )
)
echo rb: python が見つかりません（resource-broker は Python 3.13 以上を必要とします） 1>&2
exit /b 127
