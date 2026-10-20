"""Persistent backtest preferences and uploaded plans, isolated from live state."""
from dataclasses import fields
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
import csv
import json
import os

from modules.pine_levels import parse_pine
from modules.pine_stock_signals import Settings

STORE_DIR=Path(__file__).resolve().parent/'state'/'daily_pine'
FIELD_WIDGETS={field.name:'pine_bt_'+field.name for field in fields(Settings)
    if field.name not in ('execution','live_confirmation')}
FIELD_WIDGETS.update(max_positions='pine_bt_positions',max_daily_trades='pine_bt_daily_trades',
    option_max_contracts='pine_bt_option_contracts',option_allow_calls='pine_bt_calls',
    option_allow_puts='pine_bt_puts',enable_longs='pine_bt_longs',enable_shorts='pine_bt_shorts')
WIDGET_KEYS=set(FIELD_WIDGETS.values())|{'pine_bt_start','pine_bt_end','pine_bt_capital',
    'pine_bt_plans','pine_bt_date_selection','pine_bt_refresh'}


def atomic_write(path,text):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as handle:
            temporary=handle.name
            handle.write(text)
        os.replace(temporary,path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def latest_preferences():
    """Recover the user's last run settings when upgrading an existing install."""
    exports=Path(__file__).resolve().parent/'exports'
    candidates=[path for path in (exports/'pine_options'/'meta.json',exports/'pine_stocks'/'meta.json') if path.exists()]
    for path in sorted(candidates,key=lambda path:path.stat().st_mtime,reverse=True):
        try:
            meta=json.loads(path.read_text())
            settings=meta['settings']
            Settings(**settings)
            widgets={key:settings[name] for name,key in FIELD_WIDGETS.items() if name in settings}
            widgets.update(pine_bt_start=meta['start'],pine_bt_end=meta['end'],pine_bt_capital=float(meta['starting_capital']))
            trades=path.parent/'trades.csv'
            if trades.exists():
                with trades.open() as handle:
                    hashes=list(dict.fromkeys(row['plan_hash'] for row in csv.DictReader(handle) if row.get('plan_hash')))
                if hashes:
                    widgets.update(pine_bt_plans=hashes,pine_bt_date_selection=sorted(hashes))
            return dict(widgets=widgets,settings=settings)
        except (ValueError,KeyError,TypeError,OSError):
            continue
    return {}


def load_preferences():
    path=STORE_DIR/'preferences.json'
    if not path.exists():
        return latest_preferences()
    data=json.loads(path.read_text())
    if not isinstance(data,dict) or not isinstance(data.get('widgets'),dict):
        raise ValueError('Saved Pine backtest preferences are unreadable.')
    return data


def restore_widgets(state,preferences):
    for key,value in preferences.get('widgets',{}).items():
        if key not in WIDGET_KEYS or key in state:
            continue
        if key in ('pine_bt_start','pine_bt_end'):
            value=date.fromisoformat(value)
        if key=='pine_bt_date_selection':
            value=tuple(value)
        state[key]=value


def save_preferences(state,settings):
    widgets={key:state[key] for key in WIDGET_KEYS if key in state}
    data=dict(version=1,widgets=widgets,settings=settings)
    text=json.dumps(data,indent=2,sort_keys=True,default=lambda value:value.isoformat() if isinstance(value,date) else list(value))+'\n'
    path=STORE_DIR/'preferences.json'
    if not path.exists() or path.read_text()!=text:
        atomic_write(path,text)


def archive_upload(plan):
    # Date and source hash form the filename; upload filenames cannot choose a path.
    verified=parse_pine(plan.source)
    if verified!=plan:
        raise ValueError('Uploaded Pine plan differs from its source.')
    target=STORE_DIR/'scripts'/f'{plan.session_date}-{plan.source_hash}.pine'
    if not target.exists() or target.read_text()!=plan.source:
        atomic_write(target,plan.source)


def uploaded_plans():
    plans,errors=[],[]
    for path in sorted((STORE_DIR/'scripts').glob('*.pine')):
        try:
            plan=parse_pine(path.read_text())
            if path.name!=f'{plan.session_date}-{plan.source_hash}.pine':
                raise ValueError('Saved upload does not match its original source hash.')
            plans.append(plan)
        except (ValueError,OSError) as exc:
            errors.append(dict(file=path.name,error=str(exc)))
    return plans,errors
