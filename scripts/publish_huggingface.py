"""
Optional Hugging Face dataset publisher.

The script is intentionally a no-op when HF_TOKEN is absent so daily automation
can run in forks and local clones without publishing credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

from datasets import Dataset


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/published/cse_unified.parquet"


def main() -> None:
    token = os.getenv("HF_TOKEN")
    repo_id = os.getenv("HF_DATASET_REPO")
    if not token or not repo_id:
        print("Skipping Hugging Face publish: HF_TOKEN or HF_DATASET_REPO not set")
        return
    if not DATASET.exists():
        raise SystemExit(f"Missing dataset artifact: {DATASET}")

    ds = Dataset.from_parquet(str(DATASET))
    ds.push_to_hub(repo_id, token=token)
    print(f"Published dataset to Hugging Face: {repo_id}")


if __name__ == "__main__":
    main()
