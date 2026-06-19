@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python engine.py >> logs\engine_stdout.log 2>&1
