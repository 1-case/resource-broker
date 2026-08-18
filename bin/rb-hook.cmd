@echo off
rem Launch a hook without caring where python lives (cmd.exe). See bin/rb-hook for details.
rem
rem ASCII only, for the same reason as bin/rb.cmd.
rem Always exits 0: a hook must never stop the user's work.
setlocal
if "%~1"=="" exit /b 0
rem Only a bare hook name, matching the case in bin/rb-hook.
rem
rem The authority is the canonical-path comparison below, not the character tests:
rem a name containing a double quote makes the "if" lines malformed, and cmd.exe
rem skips a malformed line and carries on. %~1 is only ever expanded inside
rem set "..." and inside for (...), neither of which executes what it expands.
set "RBNAME=%~1"
set "RBHOOKS=%~dp0..\hooks"
for %%D in ("%RBHOOKS%") do set "RBHOOKS=%%~fD"
set "RBWANT=%RBHOOKS%\%RBNAME%.py"
for %%F in ("%RBWANT%") do set "RBFULL=%%~fF"
if /i not "%RBFULL%"=="%RBWANT%" exit /b 0
if not "%RBNAME%"=="%RBNAME:\=%" exit /b 0
if not "%RBNAME%"=="%RBNAME:/=%" exit /b 0
if not "%RBNAME%"=="%RBNAME:.=%" exit /b 0
if not "%RBNAME%"=="%RBNAME::=%" exit /b 0
if not exist "%RBWANT%" exit /b 0
set "RBPY="
for %%P in (py.exe python3.exe python.exe) do (
    if not defined RBPY (
        for %%F in ("%%~$PATH:P") do if not "%%~zF"=="" if %%~zF gtr 0 set "RBPY=%%P"
    )
)
if not defined RBPY exit /b 0
%RBPY% "%RBWANT%"
exit /b 0
