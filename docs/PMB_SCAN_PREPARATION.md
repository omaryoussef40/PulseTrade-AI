# PMB preparation before confirmation closes

`automation.scan_preparation.enabled` selects the prepared PMB path. It requires
enabled five-minute confirmation and the fixed PMB timeline. With preparation
enabled, missing or stale preparation blocks new PMB entries; the engine never
silently falls back to the old full-watchlist scan. Position management continues
in the parent engine. Other strategies retain their existing paths.

## Before the close

A read-only child process caches daily stock history and maintains streaming
five-minute stock bars and quotes for the watchlist. It begins option discovery
180 seconds before the next five-minute close, for stocks already strictly above
PDH (calls) or below PDL (puts). There is no proximity-based admission. The daily
levels exclude today's incomplete daily candle.

Two bounded read-only option processes discover suitable contracts without
blocking the stock stream. Discovery checks at most six market-data candidates
and twelve qualifications per lookup, respecting stricter configured limits.
It may fail to find a suitable contract; that never relaxes quality filters.
Up to three discovered alternatives can be repriced after confirmation, allowing
the order engine to apply its current budget. Failed discovery can retry after
30 seconds. Discovery records can be reused for up to ten minutes because every
executable quote is refreshed before ordering.

## After the close

The worker waits at least two seconds and requires a stock stream update at or
after the boundary, including the bar ending at that boundary. It checks that
close against PDH/PDL, then runs the existing PMB strategy over completed cached
bars. The unfinished current candle is excluded. A stock crossing late can join
from its completed candle even if it was absent from the earlier shortlist.
Missing/stale streams are omitted and reported in the preparation snapshot.

The order engine requires the exact current five-minute confirmation close,
directional PDH/PDL break, unchanged configuration, and a recent worker heartbeat.
All existing score, VWAP, ORB, ranking, position, capital, and order-safety gates
remain in place. It repeats contract qualification and uses fresh live option
quotes to recheck spread, Greeks, volume, and affordability. Delayed or unavailable
option quotes cannot authorize an entry.

Prepared confirmations expire 90 seconds after their candle close. The configured
first-scan time and entry cutoff still apply. A persisted per-candle entry claim
prevents repeated submissions/approvals as preparation results arrive or after a
restart. Risk and order execution remain with the single parent engine, avoiding
two processes placing orders or writing position state concurrently. Final risk
checks, notifications, and execution still take time; this does not promise a
fill at the candle-close timestamp or price.

## Operation and timing

The preparation file is `exports/pmb_scan_preparation.json`; it contains the
current shortlist, finalized results, prepared contracts, pending symbols and
data errors. It contains no broker or Telegram credentials. Broker clients use
the configured base client ID plus 2100 (stock streams), 2101 and 2102 (option
discovery). The children connect with read-only mode. Existing IBKR market-data
permissions and subscription limits still apply.

The parent supervises and restarts preparation when settings/watchlist change,
and stops it when automation/preparation is disabled. The worker reconnects and
refreshes daily history on a new session. Shutdown cleans up its option children.

`Prepared PMB confirmation` log entries show readiness and errors. `Prepared PMB
entry timing` entries measure the completed candle to the end of order submission
(including fill-status waiting); they are not exchange execution timestamps.
Use these alongside broker fills to measure actual live improvement.

Deploy the changed sources and merge the `scan_preparation` configuration into
the VPS's own settings. Restart the engine to load the new code. Do not replace
the VPS account/Telegram credentials with a local configuration. No deployment or
live order is performed by the local tests.
