# GAP research strategy

Research-only stock strategy. Every run pages through Yahoo's US equity screener
to build a fresh $3-$15 liquid-stock universe, then downloads Yahoo extended-hours
candles and filters for an absolute premarket gap of at least 8%, high premarket
volume/RVOL, and no verified overnight catalyst.

At 09:45 ET it fades a gap-up only after a weak first 15-minute candle below
VWAP/range midpoint, or buys a gap-down only after a strong reclaim above those
levels. Live order placement is intentionally disabled.
