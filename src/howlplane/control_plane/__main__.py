#!/usr/bin/env python3
"""
__main__.py

Package entry point for `python -m howlplane.control_plane`.
"""

import sys
from howlplane.control_plane.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
