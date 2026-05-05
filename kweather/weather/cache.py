"""Parquet-on-disk forecast cache."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


class ParquetCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, station: str, kind: str) -> Path:
        return self.root / f"{station}_{kind}.parquet"

    def write(self, station: str, kind: str, df: pd.DataFrame) -> None:
        df.to_parquet(self._path(station, kind), index=False)

    def read(self, station: str, kind: str) -> pd.DataFrame | None:
        p = self._path(station, kind)
        if not p.exists():
            return None
        return pd.read_parquet(p)
