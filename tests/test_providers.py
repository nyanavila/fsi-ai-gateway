"""
Tests for the async Anthropic provider — retry logic and streaming.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


class TestAnthropicProviderRetry:

    def _make_provider(self):
        from app.providers import AnthropicProvider
        p = AnthropicProvider.__new__(AnthropicProvider)
        p.client = MagicMock()
        return p

    def _make_response(self, text="Hello", input_tokens=10, output_tokens=20):
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
        return resp

    @pytest.mark.asyncio
    async def test_successful_call(self):
        p = self._make_provider()
        p.client.messages.create = AsyncMock(return_value=self._make_response("Hi there"))
        result = await p.complete("Hello", "You are helpful.", "claude-haiku-4-5-20251001", "trace-1")
        assert result.text == "Hi there"
        assert result.tokens_used == 30
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_then_succeeds(self):
        from anthropic import RateLimitError
        p = self._make_provider()
        p.client.messages.create = AsyncMock(side_effect=[
            RateLimitError("rate limited", response=MagicMock(status_code=429), body={}),
            self._make_response("Retry worked"),
        ])
        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await p.complete("Hi", "sys", "claude-haiku-4-5-20251001", "trace-2")
        assert result.text == "Retry worked"

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        from anthropic import RateLimitError
        p = self._make_provider()
        p.client.messages.create = AsyncMock(
            side_effect=RateLimitError("rate limited", response=MagicMock(status_code=429), body={})
        )
        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitError):
                await p.complete("Hi", "sys", "claude-haiku-4-5-20251001", "trace-3")

    @pytest.mark.asyncio
    async def test_does_not_retry_on_4xx(self):
        from anthropic import APIStatusError
        p = self._make_provider()
        err = APIStatusError("bad request", response=MagicMock(status_code=400), body={})
        p.client.messages.create = AsyncMock(side_effect=err)
        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(APIStatusError):
                await p.complete("Hi", "sys", "claude-haiku-4-5-20251001", "trace-4")
        # Should only be called once — no retry on 4xx
        assert p.client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self):
        from anthropic import APIStatusError
        p = self._make_provider()
        p.client.messages.create = AsyncMock(side_effect=[
            APIStatusError("server error", response=MagicMock(status_code=500), body={}),
            self._make_response("Back online"),
        ])
        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await p.complete("Hi", "sys", "claude-haiku-4-5-20251001", "trace-5")
        assert result.text == "Back online"

    @pytest.mark.asyncio
    async def test_token_count_is_sum(self):
        p = self._make_provider()
        p.client.messages.create = AsyncMock(
            return_value=self._make_response("Hi", input_tokens=123, output_tokens=456)
        )
        result = await p.complete("Hi", "sys", "claude-haiku-4-5-20251001", "trace-6")
        assert result.tokens_used == 579


class TestStreaming:

    @pytest.mark.asyncio
    async def test_stream_yields_chunks_and_sentinel(self):
        from app.providers import AnthropicProvider
        p = AnthropicProvider.__new__(AnthropicProvider)

        # Mock the async context manager returned by client.messages.stream
        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        async def _text_stream():
            for chunk in ["Hello", " world", "!"]:
                yield chunk

        mock_stream.text_stream = _text_stream()
        final_msg = MagicMock()
        final_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_stream.get_final_message = AsyncMock(return_value=final_msg)
        p.client = MagicMock()
        p.client.messages.stream = MagicMock(return_value=mock_stream)

        chunks = []
        async for chunk in p.stream("Hello", "sys", "claude-haiku-4-5-20251001", "trace-7"):
            chunks.append(chunk)

        text_chunks = [c for c in chunks if not c.startswith("\x00TOKENS:")]
        sentinel    = [c for c in chunks if c.startswith("\x00TOKENS:")]

        assert text_chunks == ["Hello", " world", "!"]
        assert len(sentinel) == 1
        assert sentinel[0] == "\x00TOKENS:15"
