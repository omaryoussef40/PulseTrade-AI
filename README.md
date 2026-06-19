# AutoTrader Production

Run dashboard:

```bash
streamlit run dashboard.py
```

Run engine:

```bash
python engine.py
```

The dashboard auto-saves settings to `config.json`. The engine reads the same `config.json`, writes health to `logs/health.json`, and writes daily logs to `logs/YYYY-MM-DD.log`.

Start in Simulation mode. Use Paper mode only after IB Gateway is connected to port 7497. Use Live mode only after paper testing.
