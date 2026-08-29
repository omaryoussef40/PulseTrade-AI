from __future__ import annotations

"""Yahoo market-wide universe discovery and candle loading for GAP research."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

try:
    import yfinance as yf
except Exception:  # pragma: no cover - environment dependent
    yf = None


@dataclass(frozen=True)
class YahooGapUniverseConfig:
    price_min: float = 3.0
    price_max: float = 15.0
    min_avg_daily_volume: int = 1_000_000
    page_size: int = 250
    max_symbols: int = 2_000
    exchanges: tuple[str, ...] = ("NMS", "NYQ", "ASE")


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, dict):
        value = value.get("raw", value.get("fmt"))
    try:
        return float(value)
    except Exception:
        return float(default)


class YahooGapUniverseScanner:
    """Page through Yahoo's equity screener and return every eligible symbol."""

    def __init__(self, screen_fn: Callable[..., dict[str, Any]] | None = None):
        self.screen_fn = screen_fn or (getattr(yf, "screen", None) if yf is not None else None)

    def scan(self, config: YahooGapUniverseConfig | None = None) -> pd.DataFrame:
        cfg = config or YahooGapUniverseConfig()
        if yf is None or self.screen_fn is None:
            raise RuntimeError("Yahoo market screener is unavailable. Install yfinance.")
        page_size = max(1, min(int(cfg.page_size), 250))
        max_symbols = max(1, int(cfg.max_symbols))
        query = yf.EquityQuery("and", [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("is-in", ["exchange", *cfg.exchanges]),
            yf.EquityQuery("btwn", ["intradayprice", float(cfg.price_min), float(cfg.price_max)]),
            yf.EquityQuery("gte", ["avgdailyvol3m", int(cfg.min_avg_daily_volume)]),
        ])

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        raw_quote_count = 0
        reported_total: int | None = None
        while offset < max_symbols:
            size = min(page_size, max_symbols - offset)
            response = self.screen_fn(
                query,
                offset=offset,
                size=size,
                sortField="ticker",
                sortAsc=True,
            ) or {}
            quotes = response.get("quotes") or []
            try:
                reported_total = int(response.get("total"))
            except Exception:
                pass
            if not quotes:
                break
            raw_quote_count += len(quotes)

            for quote in quotes:
                symbol = str(quote.get("symbol") or "").strip().upper()
                if not symbol or symbol in seen:
                    continue
                quote_type = str(quote.get("quoteType") or "EQUITY").upper()
                price = _number(quote.get("regularMarketPrice", quote.get("intradayprice")))
                avg_volume = int(_number(quote.get("averageDailyVolume3Month", quote.get("avgdailyvol3m"))))
                if quote_type != "EQUITY":
                    continue
                if not (float(cfg.price_min) <= price <= float(cfg.price_max)):
                    continue
                if avg_volume < int(cfg.min_avg_daily_volume):
                    continue
                seen.add(symbol)
                rows.append({
                    "symbol": symbol,
                    "name": str(quote.get("shortName") or quote.get("longName") or ""),
                    "exchange": str(quote.get("exchange") or ""),
                    "price": round(price, 4),
                    "change_pct": round(_number(quote.get("regularMarketChangePercent", quote.get("percentchange"))), 2),
                    "day_volume": int(_number(quote.get("regularMarketVolume", quote.get("dayvolume")))),
                    "avg_daily_volume_3m": avg_volume,
                    "market_cap": int(_number(quote.get("marketCap", quote.get("intradaymarketcap")))),
                    "quote_type": quote_type,
                })
                if len(rows) >= max_symbols:
                    break

            offset += len(quotes)
            if len(quotes) < size or (reported_total is not None and offset >= reported_total):
                break

        out = pd.DataFrame(rows)
        if out.empty:
            return out
        out["screened_at"] = datetime.now().isoformat(timespec="seconds")
        out["reported_total"] = reported_total
        out["raw_quotes_received"] = raw_quote_count
        out["client_filtered_count"] = max(0, raw_quote_count - len(out))
        return out.sort_values(["change_pct", "avg_daily_volume_3m"], ascending=[False, False]).reset_index(drop=True)

