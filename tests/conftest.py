import pytest


@pytest.fixture(autouse=True)
def _close_runtimes():
    """Runtimes built through tests/_runner_support are closed after each test (store lock, audit)."""
    yield
    try:
        from tests._runner_support import OPEN_RUNTIMES
    except ImportError:
        return
    while OPEN_RUNTIMES:
        OPEN_RUNTIMES.pop().close()
