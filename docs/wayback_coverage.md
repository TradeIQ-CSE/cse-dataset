# Wayback Machine Coverage

*Recovery status updated 2026-05-28.*

Earlier recon reported timeouts against the Wayback Machine CDX API from the
collection environment. That does not justify treating `tradeSummary` as a
historical replacement.

Historical OHLCV recovery must use sources with explicit report dates, such as
official CSE daily market summary PDFs, verified date-bearing CSE endpoints, or
external datasets with documented provenance and cross-source checks.
