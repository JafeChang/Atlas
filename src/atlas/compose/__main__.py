"""`python -m atlas.compose` 的入口（`-m` 要求包内有 `__main__.py`）。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
