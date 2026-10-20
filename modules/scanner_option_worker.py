"""Read-only option lookup isolated so the dashboard can enforce a deadline."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def lookup_option(payload: dict, timeout: float = 45) -> dict | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modules.scanner_option_worker"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"Option lookup exceeded {timeout:g} seconds; stock results are still available."
        ) from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1000:] or "Option lookup failed")
    response = json.loads(result.stdout)
    if response.get("error"):
        raise RuntimeError(response["error"])
    return response.get("option")


def main():
    import asyncio
    import contextlib
    import io

    asyncio.set_event_loop(asyncio.new_event_loop())
    from bot_core import IBConfig, connect_ib, recommend_option_ib

    payload = json.load(sys.stdin)
    ib = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            settings = dict(payload["ib"])
            settings["readonly"] = True
            ib = connect_ib(IBConfig(**settings))
            ib.RequestTimeout = 5
            option = recommend_option_ib(
                ib,
                payload["symbol"],
                payload["signal"],
                payload["price"],
                payload["dte"],
                payload["filters"],
            )
            clean = {key: value for key, value in option.items() if key != "Contract"} if option else None
        print(json.dumps({"option": clean}, default=str))
    except Exception as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}))
    finally:
        if ib is not None:
            ib.disconnect()


if __name__ == "__main__":
    main()
