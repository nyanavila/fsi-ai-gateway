"""
Tests for admin + streaming endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


class TestReadinessProbe:
    def test_ready_when_redis_ok(self, client):
        with patch("app.main.cache") as mock_cache:
            mock_cache.stats = AsyncMock(return_value={
                "department": "CX", "entries": 5, "backend": "hash",
                "threshold": 0.92, "ttl_seconds": 3600,
            })
            resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_not_ready_when_redis_down(self, client):
        with patch("app.main.cache") as mock_cache:
            mock_cache.stats = AsyncMock(return_value={"error": "connection refused"})
            resp = client.get("/ready")
        assert resp.status_code == 503


class TestCacheAdminEndpoints:
    def test_invalidate_known_department(self, client):
        with patch("app.main.cache") as mock_cache:
            mock_cache.invalidate_department = AsyncMock(return_value=7)
            resp = client.delete("/v1/cache/CX")
        assert resp.status_code == 200
        assert resp.json()["entries_removed"] == 7

    def test_invalidate_all(self, client):
        with patch("app.main.cache") as mock_cache:
            mock_cache.invalidate_department = AsyncMock(return_value=3)
            resp = client.delete("/v1/cache/ALL")
        assert resp.status_code == 200
        data = resp.json()
        assert "CX" in data["invalidated"]

    def test_invalidate_unknown_department(self, client):
        resp = client.delete("/v1/cache/UNKNOWN")
        assert resp.status_code == 400

    def test_cache_stats(self, client):
        with patch("app.main.cache") as mock_cache:
            mock_cache.stats = AsyncMock(return_value={
                "department": "CX", "entries": 12, "backend": "hash",
                "threshold": 0.92, "ttl_seconds": 3600,
            })
            resp = client.get("/v1/cache/CX/stats")
        assert resp.status_code == 200
        assert resp.json()["entries"] == 12


class TestBudgetEndpoint:
    def test_budget_status(self, client):
        with patch("app.main.budget") as mock_budget:
            mock_budget.status = AsyncMock(return_value={
                "CX": {"tokens_used": 100, "daily_budget": 5_000_000,
                       "fraction_used": 0.00002, "burn_rate_per_min": 5.0,
                       "reset_in_seconds": 3600},
            })
            resp = client.get("/v1/budget")
        assert resp.status_code == 200
        assert "CX" in resp.json()


class TestDLPOutputScan:
    """Verify that model output containing PII is redacted before returning."""

    def test_pii_in_response_is_redacted(self, client):
        from app.security import InjectionResult
        from app.budget import BudgetState
        from app.router import RouteResult
        from app.providers import LLMResponse

        with patch("app.main.security") as mock_sec, \
             patch("app.main.cache") as mock_cache, \
             patch("app.main.budget") as mock_budget, \
             patch("app.main.semantic_router") as mock_router, \
             patch("app.main.provider") as mock_provider:

            mock_sec.check_injection.return_value = InjectionResult(
                blocked=False, score=0.0)
            mock_sec.mask_pii.return_value = ("clean query", [])
            mock_sec.analyze_sentiment.return_value = 0.1

            # DLP scan: the model "leaked" an email — scan_output should redact it
            mock_sec.scan_output.return_value = (
                "Your account email is [EMAIL]", ["[EMAIL]"]
            )

            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()

            mock_budget.check = AsyncMock(return_value=BudgetState(
                department="CX", tokens_used=0, daily_budget=5_000_000,
                fraction_used=0.0, cx_over_threshold=False,
                hard_limit_reached=False, burn_rate_per_min=0.0,
            ))
            mock_budget.record = AsyncMock()

            mock_router.route = AsyncMock(return_value=RouteResult(
                route="CX_SIMPLE",
                model="claude-haiku-4-5-20251001",
                system_prompt="You are helpful.",
                reason="FAQ",
                confidence=0.9,
            ))

            mock_provider.complete = AsyncMock(return_value=LLMResponse(
                text="Your account email is john@example.com",
                model="claude-haiku-4-5-20251001",
                tokens_used=50,
                latency_ms=100.0,
            ))

            resp = client.post("/v1/chat", json={
                "message": "What is my account email?",
                "department": "CX",
            })

        assert resp.status_code == 200
        # The response should contain the redacted version, not the raw email
        assert "john@example.com" not in resp.json()["response"]
        assert "[EMAIL]" in resp.json()["response"]


class TestStreamingEndpoint:
    def test_stream_returns_event_stream(self, client):
        from app.security import InjectionResult
        from app.budget import BudgetState
        from app.router import RouteResult

        async def _mock_stream(*args, **kwargs):
            for chunk in ["Hello", " from", " stream", "\x00TOKENS:30"]:
                yield chunk

        with patch("app.main.security") as mock_sec, \
             patch("app.main.cache") as mock_cache, \
             patch("app.main.budget") as mock_budget, \
             patch("app.main.semantic_router") as mock_router, \
             patch("app.main.provider") as mock_provider:

            mock_sec.check_injection.return_value = InjectionResult(blocked=False, score=0.0)
            mock_sec.mask_pii.return_value = ("hi", [])
            mock_sec.scan_output.return_value = ("Hello from stream", [])
            mock_sec.analyze_sentiment.return_value = 0.0

            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()

            mock_budget.check = AsyncMock(return_value=BudgetState(
                department="CX", tokens_used=0, daily_budget=5_000_000,
                fraction_used=0.0, cx_over_threshold=False,
                hard_limit_reached=False, burn_rate_per_min=0.0,
            ))
            mock_budget.record = AsyncMock()

            mock_router.route = AsyncMock(return_value=RouteResult(
                route="CX_SIMPLE",
                model="claude-haiku-4-5-20251001",
                system_prompt="You are helpful.",
                reason="FAQ",
                confidence=0.9,
            ))

            mock_provider.stream = _mock_stream

            resp = client.post("/v1/chat/stream", json={
                "message": "Hello", "department": "CX",
            })

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "Hello" in body
        assert "[DONE]" in body
