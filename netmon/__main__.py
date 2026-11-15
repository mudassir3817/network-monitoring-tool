"""Lets the tool be run as `python -m netmon`."""
import sys

from .monitor import main

if __name__ == "__main__":
    sys.exit(main())
