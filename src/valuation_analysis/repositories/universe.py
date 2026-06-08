from __future__ import annotations

import csv
from pathlib import Path


class UniverseRepository:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def list_symbols(self) -> list[str]:
        if not self.path.exists():
            return []

        symbols: list[str] = []
        with self.path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                symbol = (row.get("symbol") or "").strip().upper()
                if symbol:
                    symbols.append(symbol)
        return symbols
