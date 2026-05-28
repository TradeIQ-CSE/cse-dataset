# Scraping Targets

*Recovery status updated 2026-05-28.*

## OHLCV

The CSE `tradeSummary` endpoint remains useful for daily current-snapshot
collection, but it is **not validated as a historical API**. The recovery
pipeline must not iterate over old dates and assign the requested date to the
response.

Current rule:

- Use `https://www.cse.lk/api/tradeSummary` only for same-day/current-trading-day
  collection.
- Accept payloads only when the observed source date equals the target date.
- Store the raw payload and metadata before accepting rows.
- Reject repeated full-market digests across different target dates unless an
  independent date-bearing source confirms the market was unchanged.

Historical backfill candidates:

- Official CSE daily market summary PDFs with parsed report date and symbol rows.
- Any official CSE API discovered later that returns date-bearing OHLCV rows.
- External/open datasets only when provenance, date coverage, and cross-source
  checks are documented.

## News

Lanka Business Online exposes a WordPress REST API:

- `https://www.lankabusinessonline.com/wp-json/wp/v2/posts`

CSE announcements can be used only when announcement dates are preserved through
the sentiment pipeline.

## Annual Reports

Annual report links should come from official CSE company financial report pages.
PDF parsing remains a separate fundamentals task and must not affect OHLCV
acceptance.
