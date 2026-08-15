@echo off
rem フックを、python の在り処を気にせず起動する（cmd.exe 用）。詳細は bin/rb-hook を参照。
rem 失敗しても 0 で戻る（フックはユーザーの作業を止めてはならない）。
setlocal
if "%~1"=="" exit /b 0
if not exist "%~dp0..\hooks\%~1.py" exit /b 0
for %%P in (python.exe py.exe) do (
    where %%P >nul 2>&1 && (
        "%%~nP" "%~dp0..\hooks\%~1.py"
        exit /b 0
    )
)
exit /b 0
