# TradeIQ EOD platform delivery

The weekday recovery workflow can push a fully accepted current-day OHLCV batch
to `market-trading`. Delivery is off by default while the platform has no
hosted HTTPS endpoint.

## Safety boundary

`daily_update.py --result-manifest PATH` records the outcome of that exact
invocation. `publish_eod.py` accepts only a non-empty result whose processed and
accepted counts match, rejected count is zero, and failure list is empty. It
does not search an accepted-data directory, so a failed collection cannot send
yesterday's file.

The publisher also requires:

- capture after the configured 14:30 Asia/Colombo market close;
- capture date equal to the requested trading date;
- trading date equal to the current Asia/Colombo date unless an operator
  supplies `--expected-trade-date` explicitly;
- one source-attributed, verified trading-day entry in an operator calendar;
- matching artifact dates, row counts and security metadata;
- no repeated market digest from the platform's latest accepted receipt. The
  digest covers canonical symbol and OHLCV content but excludes the trading
  date, so an unchanged snapshot cannot be relabelled as a later session.

The CSE `tradeSummary` response has no trustworthy source date. Its current-day
adapter therefore records `colombo_capture_date` as the source-date method. It
must never be replayed as a fetch for an earlier date.

## Calendar file

Supply a CSV with these columns:

```csv
date,is_trading_day,source,verified_at
2026-09-04,true,CSE trading calendar 2026,2026-09-01T00:00:00Z
```

The production calendar must be reviewed and source-attributed. A weekday-only
generated calendar is not acceptable because it misses exchange holidays.

## Local delivery

```sh
uv run python scripts/daily_update.py \
  --result-manifest data/run/daily_result.json

TRADEIQ_INGESTION_API_URL=http://localhost:3001 \
TRADEIQ_INGESTION_TOKEN=local-secret \
uv run python scripts/publish_eod.py \
  --result-manifest data/run/daily_result.json \
  --calendar-path /path/to/cse-trading-calendar.csv \
  --out-dir data/run/delivery
```

The output directory contains the exact request and durable platform receipt.
If a client loses the response, replay the retained request without contacting
CSE again. Replay first looks up the batch receipt and sends the retained
request only when the platform does not already have it:

```sh
uv run python scripts/publish_eod.py \
  --replay-request data/run/delivery/eod_ingestion_request.json \
  --api-url http://localhost:3001 \
  --token local-secret \
  --out-dir data/run/replay
```

Only timeouts, connection errors, rate limits and server errors are retried.
Authentication, validation and conflict failures require operator review.

## GitHub Actions configuration

After the platform is deployed, configure:

- repository variable `TRADEIQ_INGESTION_ENABLED=true`;
- secret `TRADEIQ_INGESTION_API_URL` with the HTTPS market API origin;
- secret `TRADEIQ_INGESTION_TOKEN` matching `MARKET_INGESTION_TOKEN`;
- secret `TRADEIQ_TRADING_CALENDAR_B64` containing the base64-encoded reviewed
  calendar CSV.

Run one manual workflow delivery and verify its saved receipt and platform
quote before leaving scheduled delivery enabled.
