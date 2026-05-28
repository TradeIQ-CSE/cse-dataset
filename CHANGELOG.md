# Changelog

## 0.1.0 - Recovery Baseline

- Reset the project for `cse-dataset-v2` clean-history recovery.
- Discarded generated raw, processed, published, and executed-notebook artifacts.
- Replaced the unsafe historical `tradeSummary` collector with a daily-only
  source adapter and OHLCV validation framework.
- Disabled CI publishing and generated-data commits until validation gates are
  proven.
