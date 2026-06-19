Phase 5 replacement files

Replace these files in your existing TradingBot folder:
- engine.py
- run_engine.bat
- run_dashboard.bat

Add these new files:
- install_autostart.bat
- remove_autostart.bat

What Phase 5 adds:
- Safer 24/7 engine behavior
- Engine heartbeat without connecting to IBKR when automation is disabled
- Windows auto-start scripts for the VPS
- Engine stdout logging to logs\engine_stdout.log

Local test:
python -m py_compile bot_core.py dashboard.py engine.py
python engine.py

VPS auto-start setup:
1. Copy these files into C:\autotrader or your TradingBot folder.
2. Right-click install_autostart.bat.
3. Choose Run as administrator.
4. Restart VPS or log out/in to confirm engine starts.

Important:
Keep Automation disabled until your IBKR account is approved and tested in Paper mode.
