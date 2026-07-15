"""Processed-item tracking behind a pluggable backend.

The flat-file backend matches v1 behavior. The sheet backend survives
redeploys on hosts with ephemeral filesystems (config: STATE_BACKEND=sheet).
"""

import logging
import os
from datetime import datetime, timezone

from . import config, sheets

log = logging.getLogger(__name__)


class FileStore:
    def __init__(self, filename: str):
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        self.path = os.path.join(config.OUTPUT_DIR, filename)

    def load(self) -> set[str]:
        if not os.path.exists(self.path):
            return set()
        with open(self.path, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}

    def add(self, ids) -> None:
        ids = list(ids)
        if not ids:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            for item in ids:
                f.write(f"{item}\n")


class SheetStore:
    """Processed IDs in a spreadsheet tab (column A: id, column B: timestamp)."""

    def __init__(self, worksheet_name: str):
        self.worksheet_name = worksheet_name

    def load(self) -> set[str]:
        return set(sheets.read_column(self.worksheet_name, col=1))

    def add(self, ids) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [[item, now] for item in ids]
        sheets.write_to_sheet(self.worksheet_name, rows)


def processed_posts_store():
    if config.STATE_BACKEND == "sheet":
        return SheetStore(config.SHEET_PROCESSED_TAB)
    return FileStore("processed_posts.txt")


def cramer_seen_store():
    # Cramer article URLs are low-volume; the file backend is fine everywhere,
    # but honor the sheet backend for consistency on ephemeral hosts.
    if config.STATE_BACKEND == "sheet":
        return SheetStore(config.SHEET_CRAMER_TAB + " Seen")
    return FileStore("cramer_seen.txt")
