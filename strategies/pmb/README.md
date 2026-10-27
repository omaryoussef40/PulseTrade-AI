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
1-minute and 5-minute history are aggregated into complete analysis candles
(default 15 minutes; 5 minutes for direct 5-minute confirmation). Signal timestamps record when the analysis candle closes. The shared
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

## Selectable confirmation sequence

Live **Breakout Confirmation** and Strategy Lab **Scanner** share the optional
**Require breakout confirmation** control and **Confirmation sequence** selector:

| Choice (`confirmation_mode`) | Analysis | Earliest confirmation with a 09:30–09:45 ORB |
| --- | --- | --- |
| `orb_5`: 5-minute candle after the ORB | 5 minutes | 09:50 |
| `orb_15`: 15-minute candle after the ORB | 15 minutes | 10:00 |
| `breakout_5`: 15-minute breakout, then 5-minute confirmation | 15 minutes | 10:05 |
| `legacy_next`: next candle after the breakout | Existing scanner timeframe | Existing behavior |

Direct modes use the first completed candle closing outside the ORB as the
confirmation itself, without waiting for another candle. If the initial candle
does not break out, the first later closing breakout can qualify. CALLs close
above ORB high; PUTs close below ORB low. The 5-minute direct choice calculates
score and indicators on 5-minute analysis; the other new choices use 15-minute
analysis. The opening range retains its configured duration.

`breakout_5` checks the immediately following 5-minute candle after a completed
15-minute breakout. It must close outside the ORB; the optional extension check
also requires a new directional high/low. Extension does not apply to direct
confirmation because there is no extra candle. Volume, when enabled, checks the
breakout candle against prior sessions' same-time volume on its analysis timeframe.

Existing configurations without `confirmation_mode` retain `legacy_next` behavior
and the saved `confirmation_minutes` choice (5 or 15). Saving either UI preserves
the mode and reset control; Apply to Trading Engine and Copy Live Settings carry
both. No saved live setting is changed just by upgrading the code.

Five-minute choices automatically request finer live/replay history. Live polling
is capped at the selected duration. All modes use completed candles, reject stale
confirmation, and cannot qualify from missing source candles. Entry price uses
the confirmation close. Other filters, minimum session bars, first-signal time,
and staged trading windows still apply and can postpone or prevent entry. For
example, seven minimum 15-minute session bars will block a 10:00/10:05 entry;
use two if you want those early opportunities considered. Direct 5-minute analysis
needs four session bars for a 15-minute ORB plus its first confirmation.

## Fresh breakout attempts

**Allow a fresh breakout after price closes back inside the ORB** is independent
and defaults off (`fresh_breakout_enabled`). When enabled, a completed analysis
candle closing inside the inclusive ORB boundaries re-arms the attempt. The next
closing breakout in either direction starts a fresh volume check and selected
confirmation sequence. Reset uses the analysis timeframe, rather than whichever
finer history interval was downloaded, so live and replay agree. Continuous
closes outside the range, or an intrabar touch inside, do not reset it.

Attempts are reconstructed from closed session history on every scan, including
after a restart. Trade limits, existing-position restrictions, and other filters
continue to apply. With reset disabled, each direction remains anchored to its
first breakout of the session.

## Independent breakout confirmation controls

Live settings and Strategy Lab expose two separate checkboxes:

- **Require breakout volume** (`breakout_follow_through.volume_enabled`): check
  the active opening-range breakout candle against its historical same-time
  volume baseline. This does not wait for the next candle.
- **Require breakout confirmation**
  (`breakout_follow_through.follow_through_enabled`): require the selected
  completed confirmation sequence to close beyond the opening range. The optional
  `require_extension` setting also requires a new directional extreme.

Both checks can be enabled independently. The volume check examines the
active breakout; disabling next-candle confirmation removes its timing window
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
