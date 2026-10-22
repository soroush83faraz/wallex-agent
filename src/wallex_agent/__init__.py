"""Wallex agentic BTC/USDT trader.

Money and quantities are ``decimal.Decimal`` everywhere; analytics features may be floats.
"""
from decimal import getcontext

# Largest realistic magnitudes (TMN ~1e12) times 8 decimals stay far below 50 digits.
getcontext().prec = 50

__version__ = "0.4.0"
STRATEGY_VERSION = "agentic-0.4.0"
