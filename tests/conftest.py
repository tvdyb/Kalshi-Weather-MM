"""Shared pytest fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Ensure tests don't pick up the user's real env.
os.environ.setdefault("KWEATHER_MODE", "paper")
os.environ.setdefault("KALSHI_KEY_ID", "")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", str(Path(tempfile.gettempdir()) / "no_kalshi.pem"))
os.environ.setdefault("KWEATHER_DB_PATH", str(Path(tempfile.gettempdir()) / "kweather_test.db"))
os.environ.setdefault("KWEATHER_CACHE_DIR", str(Path(tempfile.gettempdir()) / "kweather_test_cache"))


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "kweather.db"
