# PMB v2 — Pulse Momentum Breakout

## Backtester ticker universe

Strategy Lab uses the existing 34-ticker preset watchlist. Open **Symbols** to
select all presets or a subset; **Max symbols** caps the number included in a
run. Previous S&P 500 selections are restored to all 34 presets. The S&P 500
and top-100 options are no longer available.

Scheduled exits using IBKR option candles are priced from the candle at the
recorded exit time, rather than a later session closing price. Slippage and
commissions are applied to that exit price.

## Live and backtester candle alignment

Normal PMB replay evaluates completed **15-minute candles**, matching the live
normal scanner's EMA, ATR, VWAP, scoring, and breakout confirmation inputs.
The historical candle interval controls data granularity for replay and exits;
1-minute and 5-minute history are aggregated into complete 15-minute analysis
candles. Signal timestamps record when the analysis candle closes. The shared
strategy receives the same start-labeled candles as the live scanner.

Both live and replay exclude unfinished source and analysis candles. Replay
also excludes incomplete aggregate bins. Signals are evaluated at completed
analysis and selected confirmation boundaries. Minimum session bars counts analysis candles, rather than finer replay
bars. Normal stages retain the configured minimum, matching the live engine.
An extra seven calendar days of historical context warm up the indicators;
those earlier candles cannot generate trades outside the selected period.

**Use live staged timeline** requires 1-minute history for the opening scanner,
which analyzes completed 5-minute candles. Later stages use 15-minute candles.
The dashboard selects the required 1-minute historical interval automatically.
Finer historical stock candles and historical option candles remain available
for exits. This alignment does not change exit, sizing, or risk settings.

## Selectable confirmation candle

Live **Breakout Confirmation** and Strategy Lab **Scanner** expose
**Confirmation candle: 5 minutes / 15 minutes**. The selection is stored in
`breakout_follow_through.confirmation_minutes` and is copied by **Apply to
Trading Engine**. Existing normal PMB behavior defaults to 15 minutes.

Normal PMB opening range, breakout detection, indicators, and score remain on
completed 15-minute candles. The chosen confirmation candle starts immediately
after the breakout candle closes. A 09:45–10:00 breakout can qualify at 10:05
with 5-minute confirmation or 10:15 with 15-minute confirmation. It must close
outside the opening range; the optional extension rule checks its high/low
against the breakout candle. Volume still checks the original breakout candle.

Live scanning automatically fetches finer history for 5-minute confirmation.
Live scan spacing is also capped at the selected confirmation duration, so a
saved 15-minute polling interval cannot skip a 5-minute confirmation window.
Strategy Lab automatically switches 15-minute history to 5-minute history when
needed. Neither path uses an unfinished analysis or confirmation candle. Missing
confirmation bars cannot qualify, and later confirmations are stale. The entry
underlying price uses the completed confirmation candle's close, while the
score and indicators remain those of the completed analysis candles.

The setting also applies to the staged opening scanner's 5-minute analysis:
confirmation starts at the opening breakout's close and lasts the chosen
5 or 15 minutes. The staged timeline still controls whether that stage can enter.
When next-candle confirmation is disabled, this duration does not delay entries.

## Independent breakout confirmation controls

Live settings and Strategy Lab expose two separate checkboxes:

- **Require breakout volume** (`breakout_follow_through.volume_enabled`): check
  the first opening-range breakout candle against its historical same-time
  volume baseline. This does not wait for the next candle.
- **Wait for the next candle to confirm the breakout**
  (`breakout_follow_through.follow_through_enabled`): require the immediately
  next completed candle to hold beyond the opening range. The optional
  `require_extension` setting also requires a new directional extreme.

Both checks can be enabled independently. The volume check still examines the
first breakout; disabling next-candle confirmation removes its timing window
but does not introduce a new breakout or pullback entry rule. Score and risk
filters still apply. Old configs with only `enabled` retain their behavior:
true enables both checks, false disables both. Explicit toggles take precedence.
Strategy Lab saves both controls and transfers them with Apply to Live Engine.

PMB v2 is the current live strategy specification.

## Main changes

- Default ORB changed from 30 minutes to 15 minutes.
- First scanner signal defaults to 20 minutes after open.
- Confidence is no longer a trade blocker.
- The strategy now uses Score + Grade as the main setup-quality system.
- RVOL remains informational by default unless explicitly enabled as a filter or score bonus.

## Signal output

The strategy returns:

- `Signal`: CALL, PUT, or WAIT
- `Score`: PMB v2 setup score from 0 to 100
- `Grade`: A+, A, A-, B+, B, B-, C, Ignore
- `Setup Quality`
- `Score Components`
- ORB, VWAP, PDH, PDL, RVOL, ATR, EMA context

`Confidence`, `Bull Score`, and `Bear Score` may still appear as compatibility/debug fields, but they should not be used as live filters.

## Default filter

Recommended live filter:

```text
Signal = CALL or PUT
Score >= 70
ATR % >= 0.3
RVOL filter OFF by default
Confidence ignored
```

## Grade scale

```text
95-100  A+
90-94   A
85-89   A-
80-84   B+
75-79   B
70-74   B-
60-69   C
<60     Ignore
```

## PMB v2 scoring

```text
VWAP position/distance       15
EMA trend/separation         15
15m ORB break quality        20
PDH/PDL break quality        20
Breakout candle quality      10
ATR movement quality         15
Optional RVOL bonus           5
```
