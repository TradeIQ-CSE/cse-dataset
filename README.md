# Daily captures

Every run of the daily workflow (`.github/workflows/daily_update.yml` on
`main`) saves what it captured here, so captures outlive the 90-day workflow
artifacts. Nothing here is rewritten; each run adds a folder.

`<session>/<run id>/` holds one run. `<session>` is the trading session the
`tradeSummary` snapshot holds, read from its rows' `lastTradedTime`. Inside,
files keep the paths the run wrote under `data/` and `docs/`.

A session can have several runs: re-runs, and runs on holidays, which still
get the last session. Runs before 2026-09-11 dated a snapshot by the day the
run started, so paths inside their folders can name a different date from the
folder. `data/run/daily_result.json` records what a run accepted; runs before
2026-09-07 don't have one.
