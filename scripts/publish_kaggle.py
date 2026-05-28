"""
Optional Kaggle dataset publisher.

This skips cleanly unless Kaggle credentials and KAGGLE_DATASET_SLUG are present.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "data/published"


def main() -> None:
    slug = os.getenv("KAGGLE_DATASET_SLUG")
    if not slug or not os.getenv("KAGGLE_USERNAME") or not os.getenv("KAGGLE_KEY"):
        print("Skipping Kaggle publish: Kaggle env vars are not fully set")
        return
    if not PUBLISHED.exists():
        raise SystemExit(f"Missing published directory: {PUBLISHED}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in ["cse_unified.parquet", "quality_summary.json"]:
            src = PUBLISHED / name
            if src.exists():
                shutil.copy2(src, tmp_path / name)
        report = ROOT / "DATA_QUALITY_REPORT.md"
        if report.exists():
            shutil.copy2(report, tmp_path / "DATA_QUALITY_REPORT.md")
        (tmp_path / "dataset-metadata.json").write_text(json.dumps({
            "id": slug,
            "title": "Colombo Stock Exchange ML Dataset",
            "licenses": [{"name": "MIT"}],
        }, indent=2) + "\n")

        subprocess.run(
            [
                "kaggle",
                "datasets",
                "version",
                "-p",
                str(tmp_path),
                "-m",
                "Daily automated update",
            ],
            cwd=ROOT,
            check=True,
        )
    print(f"Published Kaggle dataset version: {slug}")


if __name__ == "__main__":
    main()
