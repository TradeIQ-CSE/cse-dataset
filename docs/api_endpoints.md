# CSE API Endpoints — Recon Results

Probed: 2026-02-26. Base URL: `https://www.cse.lk/api/`

## Working Endpoints

| Endpoint | Method | Key Params | Notes |
|---|---|---|---|
| `chartData` | POST (multipart/form-data) | `chartId=1`, `period=5` | Returns trailing ~1 year of ASPI daily close. `period` values: 1=1W, 2=1M, 3=3M, 4=6M, 5=1Y. No start-date override. |
| `allStock` | POST (multipart/form-data) | — | Returns all listed securities with current price snapshot. |
| `companyInfoSummery` | POST (multipart/form-data) | `symbol=COMB.N0000` | Returns beta, logo, and basic company info for a given symbol. |
| `returnAspiSnp` | GET | — | Returns `{"id":1,"status":1}` — health check only, no historical data. |
| `marketStatus` | GET | — | Returns current market open/close status. |

## Non-Working / Blocked Endpoints

| Endpoint | Status | Reason |
|---|---|---|
| `graphData` | 400 BAD_REQUEST | POST method not allowed for this URL. |
| `aspi` | 405 METHOD_NOT_ALLOWED | GET not allowed. |
| `aspi/history` | 400 BAD_REQUEST | Endpoint does not exist. |
| `aspi/daily` | 400 BAD_REQUEST | Endpoint does not exist. |
| `returnAspiSnp` (POST) | 405 METHOD_NOT_ALLOWED | Only GET is accepted. |

## Key Limitation

`chartData` is hard-capped to the trailing 1 year. No `startDate` or date-range parameter is accepted. The API response is an array of `{d: epoch_ms, v: close}` objects.

## Sample `chartData` Request

```python
import requests, datetime

r = requests.post(
    'https://www.cse.lk/api/chartData',
    files={'chartId': (None, '1'), 'period': (None, '5')},
    headers={'User-Agent': 'Mozilla/5.0'},
    timeout=15,
)
data = r.json()
# Each item: {'d': 1740441600000, 'v': 13245.67}
```
