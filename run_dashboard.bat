@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\streamlit.exe" (
  ".venv\Scripts\streamlit.exe" run dashboard.py
) else (
  streamlit run dashboard.py
)
