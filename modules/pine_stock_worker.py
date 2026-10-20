"""Keep the daily stock scan independent of the slower PMB option scan."""
import argparse
import os
import time
from .strategy_controls import effective_config


def cycle():
    from bot_core import load_config, app_log
    from .pine_stock_engine import run_pine_cycle
    cfg=effective_config(load_config())
    if not cfg.get('_concurrent_strategies'):
        return False
    if cfg.get('automation',{}).get('enabled'):
        try:
            run_pine_cycle(cfg,manage_only=bool(cfg.get('_entries_paused')))
        except Exception as exc:
            app_log(f'Daily stock worker: {exc}','WARN')
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--parent-pid',type=int,required=True)
    args=parser.parse_args()
    while True:
        try:
            os.kill(args.parent_pid,0)
        except ProcessLookupError:
            return
        if not cycle():
            return
        time.sleep(15)


if __name__=='__main__':
    main()
