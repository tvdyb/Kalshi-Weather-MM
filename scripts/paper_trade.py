"""Force paper-mode run regardless of env."""
from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    os.environ["KWEATHER_MODE"] = "paper"
    from kweather.main import cli

    sys.argv = [sys.argv[0], "--paper"]
    cli()
