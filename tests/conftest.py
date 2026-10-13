import pytest

from screener.application.metrics import metrics


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.__init__()  # type: ignore[misc]
    yield
