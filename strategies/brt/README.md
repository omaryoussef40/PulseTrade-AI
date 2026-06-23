# BRT — Break & Retest

BRT is reserved for the next strategy.

Planned logic:

- Identify key premarket high / low or major support / resistance
- Wait for a clean break
- Wait for retest
- Confirm continuation candle
- Return CALL / PUT / WAIT using the same format as PMB

For now, this module returns `None` so it cannot affect live trading.
