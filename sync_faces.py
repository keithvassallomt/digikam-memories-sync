#!/usr/bin/env python3
"""CLI entry point (thin wrapper around digikam_nextcloud package)."""
from digikam_nextcloud.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
