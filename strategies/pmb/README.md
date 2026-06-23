# PMB v2 — Pulse Momentum Breakout

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
