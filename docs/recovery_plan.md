# CSE Dataset v2 Recovery Plan

## Objective

Rebuild the project from a clean history, discard generated artifacts from the
previous repository, and recover the data pipeline in validation-first order.

## Priority Order

1. Keep only code, docs, dependency files, source schemas, and empty data
   directories.
2. Prove a daily-only OHLCV updater that never stamps requested dates onto
   unverified payloads.
3. Run source-specific OHLCV validation before writing accepted artifacts.
4. Rebuild processed OHLCV only from accepted raw payload transactions.
5. Build historical backfill separately from date-bearing sources after daily
   validation is proven.
6. Re-enable secondary pipelines and publishing only after validation gates pass.

## Current Guardrails

- `tradeSummary` is current-snapshot only unless independently date-verified.
- Historical backfill is disabled.
- Publishing to Kaggle/Hugging Face is disabled.
- CI does not commit generated data.
- Generated raw, processed, and published artifacts are ignored.
