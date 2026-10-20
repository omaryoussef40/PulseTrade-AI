# Straddle research controls

Open **Backtesting → Straddle Backtest**. Changes stay in the current form until
**Save straddle settings** or **Run Straddle Backtest**. Saving does not change
PMB, Daily Stocks, allocation or broker permissions. Straddles remain historical
research only.

**Yesterday** selects the most recently completed New York trading session,
skipping weekends and exchange holidays. After the market closes, that session
can be the current New York date. **7 days** and **14 days** use calendar-day
windows ending at the latest completed day; incomplete current sessions are
excluded. Longer one-, three- and six-month options remain available.

The simulator buys matched ATM calls and puts of the same strike and expiry.
The opening range always begins at 09:30 New York time. Set its duration, the
immediately following confirmation window, and a five-minute entry time before
confirmation finishes. The latest confirmation close decides which leg to keep.
A breakout buffer requires a larger move beyond the opening-range boundary.

Optional filters require the close to be above regular-session HLC3 VWAP for a
call or below it for a put. Confirmation volume compares average five-minute
volume in the confirmation window against average five-minute volume in the
opening range. This is separate from PMB's prior-session volume baseline.
Direction permissions can allow both directions, calls only, or puts only.
If no breakout passes the selected rules, close both at confirmation or hold
both to the configured timed exit.

Winner stop, target and trailing controls can each be disabled. Optional
breakeven moves the winner stop to its original entry premium after the chosen
profit threshold. It does not cover exit slippage or commissions. Winner exits
start only after confirmation; they are not protection for the initial two-leg
holding period. The timed exit remains active with all other exits disabled.

Contracts per leg scale both premiums, proceeds and per-contract commissions.
Each pair has two purchases and two sales. Results and export metadata record
contract quantity and all selected rules.

Historical option prices are sampled at confirmation and completed five-minute
closes. Stops use observed prices, including gaps below the stop. Targets retain
their limit level. A timed exit uses the cutoff candle's opening price when
available or a preceding completed close; it never uses the cutoff candle's
future close. Incomplete stock confirmation windows and option prices that
cannot be matched at entry or confirmation are reported or excluded rather than
invented. Sparse option history can still make timed-exit prices stale; the
backtest is a historical model, not a live fill simulation.


## Independent data cache

Straddle research uses `backtester/cache/straddle/ibkr/stocks` and
`backtester/cache/straddle/ibkr/options`. PMB retains its existing
`backtester/cache/ibkr` cache. Existing PMB files are not copied, deleted or used
by the straddle provider.

**Refresh stock and option data** bypasses cached stock bars, both option-leg
price caches and previously failed option requests. Forced stock requests do not
fall back to cached bars when IBKR fails or returns an empty response. Refreshed
sessions replace cached sessions in full, so an old afternoon does not conceal a
partial new response. The checkbox resets when its view is discarded; tick it
again whenever you want a fresh download.

Under **Straddle cached data**, **Clear selected straddle cache** removes only
selected tickers in this cache, including cached failures. Saved backtest results
and PMB data remain untouched. Run metadata records the cache namespace, the
refresh flag and stock data sources. Option pricing remains based on TRADES;
refreshing cannot create trades or history that IBKR does not supply.
