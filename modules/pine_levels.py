"""Parse the supported Daily Trade Levels Pine format as data, never execute it."""
from dataclasses import dataclass,asdict
from datetime import date
from pathlib import Path
import hashlib
import json
import math
import re
import textwrap

ROOT=Path(__file__).resolve().parents[1]
PLAN_FILE=ROOT/'exports'/'daily_stock_levels.json'
FIELDS=('longEntry','longStop','longT1','longT2','shortEntry','shortStop','shortT1','shortT2')


@dataclass(frozen=True)
class StockLevels:
    symbol: str
    longEntry: float
    longStop: float
    longT1: float
    longT2: float
    shortEntry: float
    shortStop: float
    shortT1: float
    shortT2: float

    def side(self,direction):
        prefix='long' if direction=='LONG' else 'short'
        return {key: getattr(self,prefix+suffix) for key,suffix in [('entry','Entry'),('stop','Stop'),('t1','T1'),('t2','T2')]}


@dataclass(frozen=True)
class SignalRules:
    require_volume: bool
    morning_only: bool
    volume_multiple: float = 1.3
    volume_lookback: int = 20
    morning_start: str = '09:30'
    morning_end: str = '12:00'


@dataclass(frozen=True)
class DailyPlan:
    session_date: date
    levels: tuple[StockLevels,...]
    source_hash: str
    source: str
    version: str = 'daily-pine-stocks-v1'
    signal_rules: SignalRules | None = None

    def to_dict(self):
        result=dict(session_date=self.session_date.isoformat(),levels=[asdict(r) for r in self.levels],source_hash=self.source_hash,source=self.source,version=self.version)
        # Keep already saved v1 plans compatible with their original source.
        if self.signal_rules is not None:
            result['signal_rules']=asdict(self.signal_rules)
        return result


def _rename_tokens(code, aliases):
    # Keep ticker strings, display labels, and session literals untouched.
    return re.sub(r'"(?:\\.|[^"\\])*"|\b[A-Za-z_]\w*\b',
        lambda match: aliases.get(match.group(0), match.group(0)), code)


def _canonical_date_key_study(code):
    """Recognize explicit date-key semantics instead of a particular date variable name."""
    declarations = re.findall(r'^(?:var\s+)?(?:(?:bool|float|int|string)\s+)?(\w+)\s*=\s*(.+)$', code, re.M)
    keys = []
    for name, expression in declarations:
        compact = re.sub(r'\s+', '', expression)
        match = re.fullmatch(r'year\(time,(\w+)\)\*10000\+month\(time,\1\)\*100\+dayofmonth\(time,\1\)', compact)
        if match:
            keys.append((name, match.group(1)))
    if not keys:
        return None
    if len(keys) != 1:
        raise ValueError('Multiple trading-date keys found. Paste one complete daily study.')
    day_key, timezone_name = keys[0]
    dates = [(name, expression) for name, expression in declarations
        if re.fullmatch(re.escape(day_key)+r'\s*==\s*\d{8}', expression.strip())]
    if len(dates) != 1:
        raise ValueError('Use one explicit New York date comparison: dateKey == YYYYMMDD. The importer never assumes today’s date.')
    aliases = {
        'targetDate':'correctDate', 'tradeDay':'correctDate',
        'rth':'inRth', 'sumPriceVolume':'runningPV', 'accumPV':'runningPV',
        'sumVolume':'runningVolume', 'accumVol':'runningVolume',
        'sessionVWAP':'rthVWAP', 'restrictMorning':'morningOnly',
        'inMorning':'morningRth', 'morning':'morningRth',
        'priorVolumeAvg':'average20PriorVolume', 'avg20Volume':'average20PriorVolume',
        'sessionOK':'timeOK', 'signalTime':'timeOK',
        'longCross':'crossedLong', 'shortCross':'crossedShort',
        'selectedSymbol':'symbolName', 'symbol':'symbolName',
    }
    aliases.update({day_key:'nyDayKey', timezone_name:'nyTZ', dates[0][0]:'correctDate'})
    for name, expression in declarations:
        if expression.strip() == 'timeframe.isintraday':
            if sum(other == name for other, _ in declarations) != 1 or re.search(r'\b'+re.escape(name)+r'\s*(:=|\+=|-=|\*=|/=)', code):
                raise ValueError('Intraday guard overrides are unsupported.')
            code = re.sub(r'^(?:bool\s+)?'+re.escape(name)+r'\s*=\s*timeframe\.isintraday\s*$', '', code, flags=re.M)
            aliases[name] = 'timeframe.isintraday'
    canonical = _rename_tokens(code, aliases)
    intraday_sessions = bool(re.search(r'^(?:bool\s+)?inRth\s*=\s*timeframe\.isintraday\s+and\b', canonical, re.M))
    return canonical, intraday_sessions


def parse_pine(source):
    if not isinstance(source,str) or not source.strip() or len(source.encode())>256_000:
        raise ValueError('Paste a nonempty Daily Trade Levels Pine script (maximum 256 KB).')
    code=textwrap.dedent('\n'.join(line.split('//',1)[0].rstrip() for line in source.splitlines())).lstrip('\ufeff')
    normalized = _canonical_date_key_study(code)
    if normalized is not None:
        canonical, intraday_sessions = normalized
        return _parse_daily_study(source, canonical, intraday_sessions=intraday_sessions)
    if re.search(r'^(?:var\s+)?(?:bool\s+)?targetDate\s*=',code,re.M):
        # The October 13 template uses equivalent names and puts the intraday
        # guard on the session definitions. Rename code tokens only: labels,
        # ticker strings and the retained original source must stay untouched.
        aliases={
            'targetDate':'correctDate','dayKey':'nyDayKey','tz':'nyTZ',
            'rth':'inRth','sumPriceVolume':'runningPV','sumVolume':'runningVolume',
            'restrictMorning':'morningOnly','inMorning':'morningRth',
            'priorVolumeAvg':'average20PriorVolume','sessionOK':'timeOK',
            'longCross':'crossedLong','shortCross':'crossedShort',
            'selectedSymbol':'symbolName',
        }
        canonical=re.sub(r'"(?:\\.|[^"\\])*"|\b[A-Za-z_]\w*\b',
            lambda match:aliases.get(match.group(0),match.group(0)),code)
        return _parse_daily_study(source,canonical,intraday_sessions=True)
    if re.search(r'^(?:bool\s+)?correctDate\s*=',code,re.M):
        return _parse_daily_study(source,code)
    declarations=list(re.finditer(r'^(?:var\s+)?(?:bool\s+)?activeDay\s*=[ \t]*(.*)$',code,re.M))
    if not declarations:
        raise ValueError('No activeDay definition found, and no supported correctDate or targetDate definition found. Paste the complete Pine script, including its New York trading-date line. The importer never assumes today’s date.')
    if len(declarations)!=1:
        raise ValueError('Multiple activeDay definitions found. Paste one complete script with one trading-date definition.')
    declaration=declarations[0]
    expression=declaration.group(1).strip()
    for line in code[declaration.end():].splitlines():
        if not line.strip():
            continue
        if line.startswith((' ','\t')) or line.strip().startswith('and ') or line.strip()==')':
            expression+=' '+line.strip()
        else:
            break
    # Permit wrapping parentheses, but never evaluate Pine expressions or infer dates.
    while expression.startswith('(') and expression.endswith(')'):
        depth=0
        encloses=True
        for index,char in enumerate(expression):
            depth+=int(char=='(')-int(char==')')
            if depth==0 and index<len(expression)-1:
                encloses=False
                break
        if not encloses or depth!=0:
            break
        expression=expression[1:-1].strip()
    expected=r'year\(time,\s*"America/New_York"\)\s*==\s*(\d{4})\s+and\s+month\(time,\s*"America/New_York"\)\s*==\s*(\d{1,2})\s+and\s+dayofmonth\(time,\s*"America/New_York"\)\s*==\s*(\d{1,2})'
    matched=re.fullmatch(expected,expression)
    if not matched:
        raise ValueError('Missing supported activeDay date in America/New_York. Use the attached Daily Trade Levels format.')
    try:
        session=date(*map(int,matched.groups()))
    except ValueError:
        raise ValueError('Invalid Pine trading date.') from None
    # Accept only the exact supplied signal semantics, not arbitrary Pine logic.
    required={'vwapLine':'ta.vwap(hlc3)','longCross':'ta.crossover(close,longEntry)','shortCross':'ta.crossunder(close,shortEntry)',
        'longSignal':'activeDay and timeframe.isintraday and barstate.isconfirmed and not na(longEntry) and longCross and close>vwapLine',
        'shortSignal':'activeDay and timeframe.isintraday and barstate.isconfirmed and not na(shortEntry) and shortCross and close<vwapLine'}
    for name,expression in required.items():
        matches=re.findall(r'^'+name+r'\s*=\s*(.+)$',code,re.M)
        if len(matches)!=1 or re.sub(r'\s+','',matches[0])!=re.sub(r'\s+','',expression):
            raise ValueError(f'Unsupported {name} rule. Keep the supplied crossover/VWAP/confirmed-candle logic.')
    if re.search(r'\b(activeDay|vwapLine|longCross|shortCross|longSignal|shortSignal)\s*:=',code):
        raise ValueError('Signal/date overrides are unsupported.')
    return _parse_levels(code,session,source)


def _parse_levels(code,session,source):
    rows=[]
    current=None
    values={}
    def finish():
        if current is None:
            return
        missing=set(FIELDS)-set(values)
        if missing:
            raise ValueError(f'{current}: missing levels: {", ".join(sorted(missing))}')
        if not all(math.isfinite(v) and v>0 for v in values.values()):
            raise ValueError(f'{current}: every level must be a positive finite price.')
        if not values['longStop']<values['longEntry']<values['longT1']<values['longT2']:
            raise ValueError(f'{current}: long levels must be stop < entry < T1 < T2.')
        if not values['shortT2']<values['shortT1']<values['shortEntry']<values['shortStop']:
            raise ValueError(f'{current}: short levels must be T2 < T1 < entry < stop.')
        if values['shortEntry']>=values['longEntry']:
            raise ValueError(f'{current}: short trigger must be below long trigger.')
        rows.append(StockLevels(current,**values))
    for line in code.splitlines():
        match=re.fullmatch(r'if\s+syminfo\.ticker\s*==\s*"([A-Z0-9][A-Z0-9.\-]{0,19})"',line.strip())
        if match:
            finish()
            current,values=match.group(1),{}
            continue
        assignment=re.match(r'\s*('+ '|'.join(FIELDS)+r')\s*:=\s*(.+)$',line)
        if assignment:
            if current is None or not line.startswith((' ','\t')):
                raise ValueError('Level assignments must be inside a ticker block.')
            name,value=assignment.groups()
            if name in values:
                raise ValueError(f'{current}: duplicate {name}.')
            if not re.fullmatch(r'\d+(?:\.\d+)?',value.strip()):
                raise ValueError(f'{current}: {name} must be a literal price, not a Pine expression.')
            values[name]=float(value)
        elif current is not None and line.strip() and not line.startswith((' ','\t')):
            finish()
            current,values=None,{}
        elif current is not None and line.strip() and not re.fullmatch(r'\s*tickerName\s*:=\s*"'+re.escape(current)+r'"',line):
            raise ValueError(f'{current}: unsupported line inside ticker block.')
    finish()
    if not rows or len({r.symbol for r in rows})!=len(rows):
        raise ValueError('Ticker blocks are missing or duplicated.')
    return DailyPlan(session,tuple(sorted(rows,key=lambda r:r.symbol)),hashlib.sha256(source.encode()).hexdigest(),source)

def _parse_daily_study(source,code,*,intraday_sessions=False):
    """Validate the supplied date-key/RTH/volume template without evaluating Pine."""
    def declaration(name):
        matches=re.findall(r'^[ \t]*(?:var\s+)?(?:(?:bool|float|int|string)\s+)?'+re.escape(name)+r'\s*=[ \t]*(.+)$',code,re.M)
        if len(matches)!=1:
            raise ValueError(f'Missing or duplicate {name} definition. Paste one complete daily study.')
        return matches[0]

    def require(name,expected):
        if re.sub(r'\s+','',declaration(name))!=re.sub(r'\s+','',expected):
            raise ValueError(f'Unsupported {name} rule in daily study.')

    require('nyTZ','"America/New_York"')
    require('nyDayKey','year(time, nyTZ) * 10000 + month(time, nyTZ) * 100 + dayofmonth(time, nyTZ)')
    match=re.fullmatch(r'nyDayKey\s*==\s*(\d{8})',declaration('correctDate').strip())
    if not match:
        raise ValueError('Use an explicit correctDate = nyDayKey == YYYYMMDD trading date.')
    value=match.group(1)
    try:
        session=date(int(value[:4]),int(value[4:6]),int(value[6:]))
    except ValueError:
        raise ValueError('Invalid Pine trading date.') from None
    required={
        'inRth':'not na(time(timeframe.period, "0930-1600:1234567", nyTZ))',
        'newRth':'inRth and (not inRth[1] or nyDayKey != nyDayKey[1])',
        'runningPV':'0.0', 'runningVolume':'0.0',
        'rthVWAP':'inRth and runningVolume > 0 ? runningPV / runningVolume : na',
        'morningRth':'not na(time(timeframe.period, "0930-1200:1234567", nyTZ))',
        'average20PriorVolume':'ta.sma(volume, 20)[1]',
        'volumeOK':'not requireVolume or (not na(average20PriorVolume) and volume >= 1.3 * average20PriorVolume)',
        'timeOK':'timeframe.isintraday and correctDate and inRth and (not morningOnly or morningRth)',
        'crossedLong':'ta.crossover(close, le)',
        'crossedShort':'ta.crossunder(close, se)',
        'longSignal':'barstate.isconfirmed and timeOK and not na(le) and crossedLong and not na(rthVWAP) and close > rthVWAP and volumeOK',
        'shortSignal':'barstate.isconfirmed and timeOK and not na(se) and crossedShort and not na(rthVWAP) and close < rthVWAP and volumeOK',
    }
    if intraday_sessions:
        required['inRth']='timeframe.isintraday and '+required['inRth']
        required['morningRth']='timeframe.isintraday and '+required['morningRth']
        required['timeOK']='inRth and correctDate and (not morningOnly or morningRth)'
    morning = re.fullmatch(r'(?:timeframe\.isintraday\s+and\s+)?not na\(time\(timeframe\.period,\s*"(0930|0940)-1200:1234567",\s*nyTZ\)\)', declaration('morningRth'))
    if not morning:
        raise ValueError('Unsupported morning session; use an explicit 09:30 or 09:40 to 12:00 New York window.')
    morning_start = morning.group(1)
    required['morningRth'] = required['morningRth'].replace('0930-1200', morning_start+'-1200')
    for name,expected in required.items():
        require(name,expected)
    defaults={}
    for name in ('requireVolume','morningOnly'):
        match=re.fullmatch(r'input\.bool\(\s*(true|false)\s*,\s*"[^"\n]*"\s*\)',declaration(name))
        if not match:
            raise ValueError(f'Unsupported {name} input; use the supplied literal input.bool default.')
        defaults[name]=match.group(1)=='true'
    for name in ('runningPV','runningVolume'):
        if not re.search(r'^var\s+float\s+'+name+r'\s*=',code,re.M):
            raise ValueError(f'{name} must be a persistent var float for session VWAP.')
    # Validate the stateful VWAP blocks as well as its final expression.
    blocks={
        'newRth':['runningPV := 0.0','runningVolume := 0.0'],
        'inRth':['runningPV += hlc3 * volume','runningVolume += volume'],
    }
    for condition,body in blocks.items():
        matches=re.findall(r'^if '+condition+r'\s*\n((?:[ \t]+[^\n]*\n?)+)',code,re.M)
        actual = [re.sub(r'\s+','',line) for line in matches[0].splitlines() if line.strip()] if len(matches)==1 else []
        accepted = [[re.sub(r'\s+','',line) for line in body]]
        if condition == 'inRth':
            accepted.append(['runningPV+=hlc3*nz(volume,0)', 'runningVolume+=nz(volume,0)'])
        if actual not in accepted:
            raise ValueError(f'Unsupported {condition} VWAP update block.')
    protected=set(required)|{'nyTZ','nyDayKey','correctDate','requireVolume','morningOnly'}
    writes=re.findall(r'\b('+ '|'.join(sorted(protected))+r')\s*(:=|\+=|-=|\*=|/=)',code)
    if sorted(writes)!=sorted([('runningPV',':='),('runningPV','+='),('runningVolume',':='),('runningVolume','+=')]):
        raise ValueError('Signal/date/volume overrides are unsupported.')
    if re.search(r'^(?:var\s+)?(?:bool\s+)?activeDay\s*=',code,re.M):
        raise ValueError('Multiple trading-date formats found. Paste one complete script.')
    aliases=dict(zip(('le','ls','lt1','lt2','se','ss','st1','st2'),FIELDS))
    aliases['symbolName']='tickerName'
    for name in aliases:
        if name == 'symbolName':
            if not re.fullmatch(r'"[^"\n]*"', declaration(name)):
                raise ValueError('Unsupported ticker display label; use a literal string.')
        else:
            require(name,'na')
    if re.search(r'\b('+ '|'.join(aliases)+r')\s*(\+=|-=|\*=|/=)',code):
        raise ValueError('Computed level overrides are unsupported; use literal ticker prices.')
    translated=re.sub(r'\b('+ '|'.join(aliases)+r')\b',lambda m:aliases[m.group(0)],code)
    parsed=_parse_levels(translated,session,source)
    return DailyPlan(session,parsed.levels,parsed.source_hash,source,'daily-pine-stocks-v2',SignalRules(defaults['requireVolume'],defaults['morningOnly'],morning_start=morning_start[:2]+':'+morning_start[2:]))



def save_plan(plan,path=PLAN_FILE):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    # Retain dated sources for historical replay before replacing the live plan.
    archive=path.parent/'daily_stock_plan_history'
    archive.mkdir(parents=True,exist_ok=True)
    try:
        previous=load_plan(path)
    except ValueError:
        previous=None
    for item in (previous,plan):
        if item is not None:
            target=archive/f'{item.session_date}-{item.source_hash}.json'
            target.write_text(json.dumps(item.to_dict(),indent=2)+'\n')
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(plan.to_dict(),indent=2)+'\n')
    temp.replace(path)


def load_plan(path=PLAN_FILE):
    try:
        stored=json.loads(Path(path).read_text())
    except (OSError,ValueError):
        return None
    if not isinstance(stored,dict) or not isinstance(stored.get('source'),str):
        raise ValueError('Saved plan has no Pine source. Import the script again.')
    plan=parse_pine(stored['source'])
    if plan.to_dict()!=stored:
        raise ValueError('Saved Pine levels differ from their source. Import the script again.')
    return plan
