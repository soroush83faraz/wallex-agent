"""``python -m wallex_agent`` == ``wallex-agent``."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
