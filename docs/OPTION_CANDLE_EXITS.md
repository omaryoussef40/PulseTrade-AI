# Option exits at five-minute closes

The `risk.exit_evaluation_mode` setting selects `five_minute_close` or the
legacy `snapshot` behavior. The configured profile uses breakeven at +10%,
trailing activation at +15%, and a 5% trail from the highest qualifying close.

In candle mode, the engine requests IBKR five-minute `TRADES` bars for the held
option contract once per completed boundary. IBKR start times are converted to
close times. Unfinished bars, bars ending before entry, and zero-volume carried
forward prices do not make exit decisions. Failed data requests are retried;
snapshot marks remain available for the position display but cannot tighten or
trigger virtual stops. A slow scanner or restart can delay checks; missed closes
are evaluated in order, and any resulting market exit happens at the current
market, never at an assumed historical stop fill.

The initial broker stop stays separate from virtual breakeven/trailing levels.
The current profile keeps it 10% below entry. It can trigger between candle
closes. Switching an existing automated position into candle mode resets its
virtual state and restores bot-managed broker stops to this emergency level;
explicit manual broker overrides remain respected. Manual positions stay
monitor-only. Premium-health tightening, trail-from-entry and automatic fixed
profit targets do not apply in candle mode. End-of-day exit approval remains
independent of the five-minute schedule.

Exit submission retains a pending position. The close notification waits until
broker reconciliation confirms the position is flat and the execution import
has all matching commission reports. The message then uses the actual weighted
execution price and broker net P/L, including fees. Missing data stays pending
instead of appearing as zero profit. Partial or working exits cannot cause a
second software sell. Known rejected/cancelled exits resume protection for the
remaining position.

Deploy the updated Python sources to the VPS and merge the risk fields into its
own configuration; do not replace account credentials with a local configuration.
The Python processes need a restart to load the new behavior. The configuration
alone does not add candle-close exits to an older running process.
