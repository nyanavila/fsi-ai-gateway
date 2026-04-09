"""
Shared pytest fixtures and configuration.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_anthropic_client():
    """Returns a mock Anthropic client whose messages.create returns a
    configurable text payload."""
    client = MagicMock()

    def _make_response(text: str):
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.usage = MagicMock(input_tokens=50, output_tokens=100)
        return resp

    client._make_response = _make_response
    client.messages.create = MagicMock(
        return_value=_make_response('{"route":"CX_SIMPLE","reason":"FAQ","confidence":0.9}')
    )
    return client


@pytest.fixture
def mock_redis():
    """Async Redis mock with sensible defaults."""
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock(return_value=True)
    r.setex = AsyncMock(return_value=True)
    r.delete = AsyncMock(return_value=1)
    r.keys = AsyncMock(return_value=[])
    r.incrby = AsyncMock(return_value=100)
    r.zadd = AsyncMock(return_value=1)
    r.zremrangebyscore = AsyncMock(return_value=0)
    r.zrangebyscore = AsyncMock(return_value=[])
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.set = AsyncMock()
    pipe.delete = AsyncMock()
    pipe.execute = AsyncMock(return_value=[True, True, 0])
    r.pipeline = MagicMock(return_value=pipe)
    return r


@pytest.fixture
def normal_budget_state():
    from app.budget import BudgetState
    return BudgetState(
        department="CX", tokens_used=100_000, daily_budget=5_000_000,
        fraction_used=0.02, cx_over_threshold=False,
        hard_limit_reached=False, burn_rate_per_min=10.0,
    )


@pytest.fixture
def over_budget_state():
    from app.budget import BudgetState
    return BudgetState(
        department="CX", tokens_used=5_500_000, daily_budget=5_000_000,
        fraction_used=1.10, cx_over_threshold=True,
        hard_limit_reached=False, burn_rate_per_min=2000.0,
    )


@pytest.fixture
def hard_limit_budget_state():
    from app.budget import BudgetState
    return BudgetState(
        department="CX", tokens_used=7_500_001, daily_budget=5_000_000,
        fraction_used=1.50, cx_over_threshold=True,
        hard_limit_reached=True, burn_rate_per_min=5000.0,
    )
