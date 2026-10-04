@echo off
cd /d "%~dp0"
set "SIGNER_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if exist "%SIGNER_PYTHON%" (
    start "" "%SIGNER_PYTHON%" "%~dp0app.py"
) else (
    python "%~dp0app.py"
)
