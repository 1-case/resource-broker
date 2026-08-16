@echo off
rem Launch the resource-broker CLI without installing it (cmd.exe). See bin/rb for the rationale.
rem
rem ASCII only. cmd.exe reads batch files in the console code page (cp932 on Japanese
rem Windows), so UTF-8 comments get mangled and stray bytes are executed as commands.
rem
rem Do NOT use `setlocal enabledelayedexpansion`: `%*` is expanded into the block first and
rem then rescanned for `!...!`, so a `!` inside an argument is silently eaten. `--observed`
rem is the one thing this tool enforces; it must not break quietly.
rem Reading %ERRORLEVEL% after the loop (not inside it) returns the real exit code.
setlocal
set "RBPY="
for %%P in (py.exe python3.13.exe python3.12.exe python3.11.exe python3.exe python.exe) do (
    if not defined RBPY (
        rem A Microsoft Store alias is a 0-byte stub that `where` finds but cannot run.
        rem Asking for the version proves it is real and new enough in one step.
        where %%P >nul 2>&1 && %%P -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "RBPY=%%P"
    )
)
if not defined RBPY (
    echo rb: no Python 3.11+ found 1>&2
    exit /b 127
)
%RBPY% "%~dp0rb.py" %*
exit /b %ERRORLEVEL%
