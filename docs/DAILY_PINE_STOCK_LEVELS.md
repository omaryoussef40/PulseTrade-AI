# Daily Pine stock levels

Open **Strategies → Daily Stocks → Daily Plan**. Paste your daily Pine script
or upload a `.pine` file. All ticker levels appear immediately. You can paste,
review and save a plan at any time without connecting to IBKR.

Review and save the levels, then open **Trading controls**. Choose **Daily Pine
Levels — stocks** as the strategy allowed to open new trades. Set the cumulative
daily entry budget, value per trade, stop risk per trade, daily loss limit, trade
count, open-position count, permitted tickers and long/short permissions. Save
the strategy selection and limits. Loading or saving a script alone does not
activate trading when strategy controls are enabled.

Choose Simulation or IBKR Paper in **Live Trading → Controls**. Restart an already-running
engine once to load this update, then use the existing automation and account/risk
approval controls. Keep TWS or IB Gateway connected for data. Live execution requires
the separate approvals below. The legacy intraday scanner remains available for
manual use.

Trading controls support individual profiles or **PMB options + Daily Pine stocks**
together. The combined mode defaults to 80% of verified USD account equity for
PMB option premium and 20% for stocks, with a 2× stock margin multiplier. On a
$10,000 account this permits up to $8,000 of PMB premium and $4,000 of stock
position value. Broker available funds and existing position/risk limits can
reduce those amounts. Buying power is never used as the allocation denominator.
Missing verified equity blocks new combined-mode entries.

Combined mode preserves PMB signal, exit, per-trade and trade-count settings;
only its total capital allocation changes. Existing PMB capital recycling is
preserved. Stocks retain their cumulative daily entry and stop-risk limits in
addition to the allocation cap. Stock and option risk counters are separate.
The stock scanner runs in a separate child process every 15 seconds so PMB scans
do not delay its five-minute confirmations. Pause keeps both position monitors
running. Restart the engine to load the new worker.

The Pine profile supports shares or 7-DTE calls/puts; PMB and Break & Retest trade options. Each profile saves separate
budget and ticker permissions. PMB and Break & Retest still share the existing
option ledger: all option entries that day count against the selected profile's
caps, so switching profiles never resets spending or trade count. Strategy
controls in single-strategy mode override the separate opening-ORB and staged-timeline presets.

**Pause automatic entries** keeps existing positions monitored under the existing
execution permissions. Removing an active strategy is blocked while recorded
positions are open or unresolved. Choosing a strategy never enables global broker
order permissions.

The stock daily entry budget measures cumulative purchase/short-entry value,
not losses or current open exposure. Closing a position does not replenish it.
Confirmed cancelled orders with no fills release their reservation; unresolved
orders retain it. The daily loss limit is separate. Existing combined exposure
and stop-risk limits in **Paste & settings** also apply. Budgets reset by New York
entry date and apply separately to the execution account/mode's stock ledger.

## Supported daily study formats

The importer accepts the original `activeDay` format and the Orora study format
with `correctDate = nyDayKey == YYYYMMDD`, `le`/`ls`/`lt1`/`lt2` and the equivalent
short-level names. The trading date always comes from the script.

The October 13 format is also supported: `targetDate = dayKey == YYYYMMDD`,
`selectedSymbol`, `restrictMorning`, and its regular-session VWAP accumulator
names. It preserves that script's input defaults, including an optional volume
filter set to false. The importer validates the equivalent signal rules and
retains the complete original source; it does not infer dates from filenames.

The October 14 format is supported as well, including `tradeDay`, `nyDay`,
`isIntraday`, `sessionVWAP`, and `nz(volume, 0)` accumulation. Date-key studies
are recognized by their explicit year/month/day calculation and literal date
comparison, so renaming the date, date-key, or timezone variable does not break
the import. The October 14 script's morning window starts at **09:40**, and that
start time is retained for both backtesting and live signal evaluation. Missing
dates, duplicate dates, and unsupported rule changes remain errors.

The Orora format preserves the `requireVolume` and `morningOnly` input defaults:
volume must be at least 1.3 times the average of the **20 preceding completed
candles**, excluding the signal candle; morning signals require a candle opening
from 09:30 through just before 12:00 New York time. A five-minute candle starting
at 11:55 may confirm at 12:00. VWAP resets each regular session and uses HLC3.
The engine's entry window and long/short permissions still apply.

Trading uses regular-session candles, including prior sessions for the volume
baseline. Compare with a TradingView chart set to regular trading hours; an
extended-hours chart can have a different prior-volume average. Missing prior
volume data blocks volume-filtered entries. Unsupported changes to signal rules
are rejected instead of silently dropped. Pine plotting, labels and PDH/PDL
visual references do not add entry conditions.

## Backtesting the saved strategy

Open **Backtesting → Daily Pine**. The backtester lists the saved daily
plan and archived plans, and also accepts historical `.pine` files. Select one
version for each date, a date range, starting capital and stock risk limits,
then choose **Run Daily Pine backtest**. Historical candles come from a read-only
IBKR connection, with seven extra calendar days loaded for the prior-session
close and volume baseline. The stock settings provide the initial risk limits;
editing backtest controls does not save live settings.

Backtest changes save automatically, including plan selections, dates, capital,
instrument, premium/contract limits and other controls. They restore after a
page refresh or leaving and returning to the workspace. Valid uploaded Pine
scripts are retained in the backtest library and do not need to be uploaded
again. Duplicate sources are stored once; separate versions are kept by source
hash. Uploaded backtest plans never replace the live daily plan. Preferences
and uploaded sources are stored under `backtester/state/daily_pine`.

Each plan opens entries only on its original date. Sessions without a plan can
manage existing positions but cannot generate new entries. Daily plans are
archived when saved, including the previously saved plan, so future runs can use
the original levels. Older history must be supplied using the original dated
scripts; the backtester never applies one day's levels to other dates.

The existing confirmed-candle crossover, regular-session VWAP, optional volume
filter and morning filter generate signals. An eligible signal fills at the next
five-minute candle's open with adverse slippage, subject to stock risk, capital,
position and trade limits. Historical bid/ask spreads are not available in this
model. T1 and T2 keep the configured share split. Entry and exit commissions are
charged per target leg, matching the stock simulation's order structure.

A bar touching both stop and target uses the stop first. Opening gaps through
stops fill at the adverse opening price with slippage; targets use their original
limit prices. Positions remain open until an exit, including overnight and at the
end of the selected test. Realized P/L, unrealized P/L and open positions are
reported separately. Short tests assume borrow availability and exclude borrow
fees.

Missing or incomplete sessions disable that session's entries and appear under
**Data issues**. A data gap while holding a position leaves the position unresolved
instead of inventing an exit across the gap. Reports, fills and downloadable CSVs
are stored under `backtester/exports/pine_stocks`, separately from PMB results and
the live stock ledger.

## Seven-day options from Pine levels

Choose **7-DTE calls / puts** under **Trade instrument** in Daily Pine Setup or
the Daily Pine backtester. A confirmed buy signal buys a call; a confirmed sell
signal buys a put. The contract uses the listed expiration closest to seven
calendar days from entry and the nearest qualifying at-the-money strike. The
actual expiry, strike and right are recorded; an exactly seven-day expiry may
not be listed. No short option position is opened.

Option premium limits, maximum contracts, position counts and daily entry/loss
limits govern sizing. The full purchase premium is reserved as risk. Share
stop-risk budgets and stock margin do not increase option size. The target split
applies to contracts; a single-contract position exits entirely at T1.

The underlying stock's original Pine stop/T1/T2 prices trigger option exits;
there are no percentage-based premium stops or trailing rules. Broker entries
are buy limit orders. Each target portion has stock-price-conditioned market
sell orders for its stop and target in an OCA group, plus an expiration-session
exit five minutes before the regular close. Live market fills depend on the
option market at trigger time. Pine-owned option positions are excluded from
PMB's percentage exit manager, while still blocking duplicate option exposure
in the same underlying.

The backtester requires historical option candles and never estimates premiums
from stock returns. Option entries and exits require actual trading volume in
the corresponding candle: a zero-volume bar can carry an older trade price and
does not establish an executable fill. An unpriced triggered exit is reported
as unresolved; stale end-of-test marks also flag the result. Reports from older
fill models display an unverified-pricing warning and must be rerun.
The full contract quantity must fit the recorded volume, counting entries and
all target portions together within each candle. If it does not, the backtester
rejects the entry or leaves the triggered exit unresolved. Even sufficient trade
volume does not prove that the displayed price was available for that quantity;
bid/ask quotes and finer timestamps are needed to assess actual execution.
It buys at the next option candle's opening premium with
adverse slippage. For an intrabar stock-level trigger, it uses the option candle's
low with adverse slippage; exact stock/option event ordering within a five-minute
bar is unavailable. Opening triggers use the option opening price. Open positions
use the option closing premium for their mark. Missing history blocks an entry
or leaves a held position unresolved. A carried position always loads the same
expiry and strike. Option results are saved in `backtester/exports/pine_options`.

The backtester defaults to options. The running live strategy's instrument is
changed explicitly in Setup and uses the existing account/automation gates.
Live options require **TRADE PINE OPTIONS LIVE** in Live Trading → Controls;
the stock confirmation does not authorize live option entries. Restart an
already-running engine after loading the code update. Broker acceptance and
execution still require verification in IBKR Paper.

## Ticker charts

The **Strategies → Daily Stocks → Charts** view shows every ticker in a two-column chart wall. Each five-minute
candlestick chart overlays the exact long and short entries, stops, T1 and T2,
with session labels and regular-market VWAP. You can pan and zoom each chart independently.
Charts refresh every five minutes by default, with a manual refresh button.

The chart date defaults to the Pine plan's session. Change **Chart session (New
York)** to review another trading session. A warning identifies charts using
levels from a different date; this never changes the engine's date restriction.
Completed premarket, regular-market and postmarket candles are shown, using
extended-hours history from 04:00 to 20:00 ET where available. The chart timeline
contains only actual candles: overnight closures, holidays and empty intervals
take no horizontal space. All times display in New York regardless of your browser's
timezone. Market VWAP uses only regular-session candles and resets at the opening;
missing regular-session bars withhold it. Early-close sessions use the exchange
calendar to identify the start of postmarket.

Chart history comes from your configured IBKR connection through a read-only
worker. Keep TWS or IB Gateway open with historical stock data permissions.
If data is unavailable, the ticker's imported level lines remain visible with a
message; candles are never invented. Viewing and refreshing charts cannot submit
orders. Unsaved pasted levels can be previewed on charts, but must be saved before
the trading engine uses them.

## Emailed daily brief

The **Daily Brief** view is prepared for `hello@orora.me` through Microsoft 365.
It needs a Microsoft app registration and sign-in before accessing the mailbox.
See [the email connection guide](DAILY_EMAIL_BRIEF.md) for setup, matching rules
and importing Pine attachments or code from the email body.

## Rules

- Uses the exact literal ticker prices and New York `activeDay` or `correctDate`
  from the supported formats. Expressions or different signal rules are rejected; Pine is never executed.
  The date definition can be typed (`bool activeDay` or `var bool activeDay`)
  or continued across lines, including wrapping parentheses. Missing or duplicate
  date definitions are reported separately. Paste the complete script including
  the date definition; the importer never assigns today's date to undated levels.
- A completed five-minute close must cross the entry and be above session VWAP
  for a long, or below it for a short. Use regular-hours five-minute charts for
  comparison. VWAP starts at 09:30 ET. The first candle can cross the preceding
  session's final close; missing history prevents that entry.
- New entries are allowed only on the plan's trading date, on an exchange trading
  session and within the configured entry window. Holidays, daylight saving and
  early closes use the XNYS calendar. Incomplete and stale candles are excluded.
- Buys or sells whole **shares**. Uses the imported stock stop and exits the
  configured fraction at T1, the remainder at T2. A one-share position exits at T1.
  Stops remain at their imported values. Positions are not automatically closed
  at the end of the session. A replacement plan does not change existing exits.
- Dollar stop-risk budgets determine share quantity; they do not replace the
  Pine stop or target prices. Daily loss limits block new entries rather than
  liquidating existing positions at a dollar amount. The live engine always
  requests five-minute entry candles, including when older settings saved a
  different interval. A wick through an entry level or a close exactly on the
  level cannot confirm an entry.
- Short entries default off. Broker short entries also require reported borrowable
  shares. Missing, delayed, frozen, stale and crossed quotes block entries.
- Share sizing reserves risk at the worst permitted entry price. Position count,
  combined stop risk, notional exposure, cumulative daily entry value, daily trade count and daily loss limits
  block additional entries. The daily loss gate includes today's realized results and conservatively includes losses on all open holdings; open gains do not offset that limit. These budgets cover this strategy's ledger, not unrelated account holdings. The kill switch blocks entries while preserving exits.
- Allows at most one attempted trade per symbol, date and execution account.
  Durable intents are written before broker submission. Unresolved submissions or
  unprotected positions freeze additional entries until reconciled; they are not retried.

## Execution and approval

**Simulation is the default**: it uses IBKR stock data and records hypothetical
fills without sending orders. Simulation assumes fills at the adverse entry limit,
exits targets at their specified prices, and applies configurable stop slippage
and fees. Quote-based simulation can miss a stop or target touched between checks;
it does not establish real execution quality or profitability.

**IBKR Paper** sends stock orders only when the global account mode is Paper and
all existing automation, order-placement and risk-approval gates are enabled.
**IBKR Live** additionally requires global Live approval and the separate phrase
`TRADE STOCK LEVELS LIVE`. No broker orders are enabled by importing a script.
Simulation and Paper use the configured paper connection; Live uses the live connection.
The engine uses an additional client ID (configured ID + 107).

Each target allocation has its own fill-or-kill limit entry, attached GTC target
and GTC stop, restricted to regular hours. Target and stop share an OCA reduction
group. Each allocation can independently fill or cancel; therefore total shares
can be smaller than requested. Stop orders can fill beyond their stop price.
This broker integration needs validation in IBKR Paper before enabling Live;
its order construction and reconciliation are tested with mocks, not actual fills.
Existing option positions are not managed by this stock strategy.

## Saved records

- `exports/daily_stock_levels.json`: exact source, source hash, date and parsed levels.
- `data/pine_stock_trades.sqlite`: stock trades, durable order keys and audit events.
- `exports/pine_stock_status.json`: latest check, blocked entries and positions shown
  on the page. These records are separate from the legacy option trade log.
- `examples/daily_trade_levels_2026-10-08.pine`: your supplied example, initially
  shown in the paste box. Its October 8 date is preserved; use a newly dated daily
  script for subsequent sessions.

Run stock and chart checks with `.venv/bin/python -m pytest tests/test_pine_stock_levels.py tests/test_pine_stock_charts.py`.

## Validation

94 targeted tests passed: the stock-level and chart tests, plus existing position
monitoring, manual exit, same-symbol limit, execution-sync and broker cache checks.
Python compilation, the paste/preview UI and all 10 chart cards passed validation
with mocked data. The corrected UTC historical request was also verified against
IBKR for the October 8 plan: all 10 symbols returned 192 candles each, spanning
04:00–19:55 ET, with premarket, market and postmarket labels. TWS rejected the
previous America/New_York request suffix (error 10314); chart requests now use
IBKR's UTC dash format. Chart-feed availability still depends on connectivity and permissions. No actual broker orders were
submitted. The separate pre-existing `test_live_completed_candles.py` could not
be collected because it imports a missing `bot_core.completed_live_bars` helper;
that unrelated implementation was not changed in this work.
