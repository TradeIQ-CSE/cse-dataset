"""
Execute baseline notebooks as a secondary ML-usability check.

Outputs executed copies under notebooks/executed/ so source notebooks remain
stable unless a user explicitly wants to commit rendered outputs.
"""

from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_DIR = ROOT / "notebooks"
OUTPUT_DIR = NOTEBOOK_DIR / "executed"


def execute_notebook(path: Path) -> None:
    nb = nbformat.read(path, as_version=4)
    client = NotebookClient(
        nb,
        timeout=900,
        kernel_name="python3",
        allow_errors=False,
        resources={"metadata": {"path": str(NOTEBOOK_DIR)}},
    )
    client.execute()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, OUTPUT_DIR / path.name)
    print(f"Executed {path.name}")


def main() -> None:
    for path in sorted(NOTEBOOK_DIR.glob("*.ipynb")):
        execute_notebook(path)


if __name__ == "__main__":
    main()
