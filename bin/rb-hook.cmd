@echo off
rem フックを、python の在り処を気にせず起動する（cmd.exe 用）。詳細は bin/rb-hook を参照。
rem 必ず 0 で戻る（フックはユーザーの作業を止めてはならない）。
setlocal
if "%~1"=="" exit /b 0
if not exist "%~dp0..\hooks\%~1.py" exit /b 0
for %%P in (py.exe python3.exe python.exe) do (
    where %%P >nul 2>&1 && (
        %%P -c "" >nul 2>&1 && (
            %%P "%~dp0..\hooks\%~1.py"
            exit /b 0
        )
    )
)
exit /b 0
