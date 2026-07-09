@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" engine.py >> logs\engine_stdout.log 2>&1
) else (
  python engine.py >> logs\engine_stdout.log 2>&1
)
