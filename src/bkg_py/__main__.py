"""Module launcher for ``python -m bkg_py``."""

from __future__ import annotations

from .cli import entrypoint

if __name__ == "__main__":
    entrypoint()
