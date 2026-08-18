@echo off
rem Launch a hook without caring where python lives (cmd.exe). See bin/rb-hook for details.
rem
rem ASCII only, for the same reason as bin/rb.cmd.
rem Always exits 0: a hook must never stop the user's work.
setlocal
if "%~1"=="" exit /b 0
rem Bare hook name only, matching the case in bin/rb-hook. Quoting already blocks
rem command injection; these four block a path traversal out of the hooks directory.
set "RBNAME=%~1"
if not "%RBNAME%"=="%RBNAME:\=%" exit /b 0
if not "%RBNAME%"=="%RBNAME:/=%" exit /b 0
if not "%RBNAME%"=="%RBNAME:.=%" exit /b 0
if not "%RBNAME%"=="%RBNAME::=%" exit /b 0
if not exist "%~dp0..\hooks\%~1.py" exit /b 0
set "RBPY="
for %%P in (py.exe python3.exe python.exe) do (
    if not defined RBPY (
        for %%F in ("%%~$PATH:P") do if not "%%~zF"=="" if %%~zF gtr 0 set "RBPY=%%P"
    )
)
if not defined RBPY exit /b 0
%RBPY% "%~dp0..\hooks\%~1.py"
exit /b 0
