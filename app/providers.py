"""
Anthropic provider — async client with:
  - Exponential backoff retry (rate limits + 5xx)
  - Streaming support for low-latency CX responses
  - Token counting on both streaming and non-streaming paths
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

import anthropic
from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError, RateLimitError

from .config import settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY  = 1.0      # seconds; doubles each retry
MAX_TOKENS  = 1024


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_used: int
    latency_ms: float


class AnthropicProvider:
    def __init__(self):
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    # ── Non-streaming ─────────────────────────────────────────────────────────

    async def complete(
        self,
        message: str,
        system_prompt: str,
        model: str,
        trace_id: str,
    ) -> LLMResponse:
        last_exc: Exception | None = None
        delay = BASE_DELAY

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self.client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": message}],
                )
                latency_ms = (time.monotonic() - start) * 1000
                text = response.content[0].text
                tokens = response.usage.input_tokens + response.usage.output_tokens

                logger.info("LLM call succeeded", extra={
                    "trace_id": trace_id,
                    "model": model,
                    "attempt": attempt,
                    "tokens": tokens,
                    "latency_ms": round(latency_ms, 1),
                })
                return LLMResponse(
                    text=text,
                    model=model,
                    tokens_used=tokens,
                    latency_ms=round(latency_ms, 1),
                )

            except RateLimitError as e:
                last_exc = e
                logger.warning(f"Rate limited (attempt {attempt}/{MAX_RETRIES}), retry in {delay}s",
                               extra={"trace_id": trace_id})
                await asyncio.sleep(delay)
                delay *= 2

            except APIConnectionError as e:
                last_exc = e
                logger.warning(f"Connection error (attempt {attempt}/{MAX_RETRIES}): {e}",
                               extra={"trace_id": trace_id})
                await asyncio.sleep(delay)
                delay *= 2

            except APIStatusError as e:
                if e.status_code >= 500:
                    last_exc = e
                    logger.warning(f"Provider {e.status_code} (attempt {attempt}/{MAX_RETRIES})",
                                   extra={"trace_id": trace_id})
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(f"Provider {e.status_code} — not retrying: {e.message}",
                                 extra={"trace_id": trace_id})
                    raise

        logger.error(f"All {MAX_RETRIES} attempts failed", extra={"trace_id": trace_id})
        raise last_exc  # type: ignore[misc]

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def stream(
        self,
        message: str,
        system_prompt: str,
        model: str,
        trace_id: str,
    ) -> AsyncIterator[str]:
        """
        Yields text chunks as they arrive from the API.
        Caller is responsible for accumulating tokens for budget accounting.
        """
        logger.info("Starting streaming LLM call", extra={
            "trace_id": trace_id, "model": model
        })
        async with self.client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk

            # Final message carries usage counts
            final = await stream.get_final_message()
            tokens = final.usage.input_tokens + final.usage.output_tokens
            logger.info("Streaming call complete", extra={
                "trace_id": trace_id,
                "model": model,
                "tokens": tokens,
            })
            # Emit a sentinel with token count so the caller can do budget accounting
            yield f"\x00TOKENS:{tokens}"
