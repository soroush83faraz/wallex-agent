"""Exception hierarchy. The split between definitive and unknown outcomes is load-bearing:

* ``RejectedError``            -> the exchange definitively refused; no order exists.
* ``TransportError`` / ``ServerError`` on a WRITE -> outcome UNKNOWN; never blind-retry.
"""
from __future__ import annotations


class WallexAgentError(Exception):
    """Base class for all package errors."""


class ConfigError(WallexAgentError):
    pass


class ExchangeError(WallexAgentError):
    """Anything that went wrong talking to (or simulating) the exchange."""


class TransportError(ExchangeError):
    """Network failure / timeout / connection reset. For a write, the outcome is unknown."""


class ServerError(ExchangeError):
    """HTTP 5xx. For a write, the outcome is unknown."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body[:500]


class RateLimitError(ExchangeError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class AuthError(ExchangeError):
    """401/403 or missing key. Never retried."""


class RejectedError(ExchangeError):
    """Definitive client-side rejection (4xx). The order does NOT exist on the exchange.

    ``error_code`` is Wallex's ``result.error_code`` for HTTP 422 (1001..1011), if present.
    """

    def __init__(self, status: int, message: str = "", error_code: int | None = None) -> None:
        super().__init__(f"rejected HTTP {status} code={error_code}: {message[:200]}")
        self.status = status
        self.message = message
        self.error_code = error_code


class OrderNotFound(ExchangeError):
    """GET order returned 404 / empty. NOT proof the order was never placed (see execution)."""


class SchemaError(ExchangeError):
    """Response did not match the schema we verified. Fail closed."""


class ForbiddenEndpointError(ExchangeError):
    """Attempt to call an endpoint outside the allow-list (withdrawals, transfers, ...)."""


class LiveNotAllowedError(WallexAgentError):
    """A live-trading gate is not satisfied."""


class InvariantError(WallexAgentError):
    """Internal invariant violated (e.g. illegal state transition). Treated as fatal -> STOPPED."""


class KillSwitchActive(WallexAgentError):
    """Operator kill switch engaged; no new orders may be sent."""
