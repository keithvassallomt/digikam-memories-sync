#!/usr/bin/env python3
"""CLI entry point (thin wrapper around digimem package)."""
from digimem.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
