"""
FSI AI Gateway — FastAPI application
Request pipeline (in order):
  1. Security: injection check → PII mask
  2. Cache: semantic lookup (Redis)
  3. Budget: quota check
  4. Router: classify intent → select model
  5. Provider: Anthropic API call (async, with retry)
  6. DLP: output PII scan
  7. Budget: record spend
  8. Cache: store response
  9. Observability: metrics + sentiment logging
"""

import time
import uuid
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .router import SemanticRouter
from .cache import SemanticCache
from .security import SecurityLayer
from .budget import BudgetManager
from .providers import AnthropicProvider
from .observability import metrics, setup_logging, RequestLogger

logger = logging.getLogger(__name__)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("AI Gateway starting up", extra={
        "version": "1.0.0",
        "env": __import__("os").getenv("APP_ENV", "unknown"),
    })
    yield
    logger.info("AI Gateway shutting down")


app = FastAPI(
    title="FSI AI Gateway",
    description="Production-grade AI Gateway for FSI customer support",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

# Singleton layer instances
semantic_router  = SemanticRouter()
cache            = SemanticCache()
security         = SecurityLayer()
budget           = BudgetManager()
provider         = AnthropicProvider()


# ── Request / Response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str        = Field(..., min_length=1, max_length=8000)
    department: str     = Field("CX", pattern="^(CX|IT|FINANCE)$")
    customer_id: str | None = None
    session_id:  str | None = None


class ChatResponse(BaseModel):
    trace_id:    str
    response:    str
    route:       str
    model_used:  str
    cache_hit:   bool
    tokens_used: int
    latency_ms:  float


class CacheStatsResponse(BaseModel):
    department: str
    entries:    int
    backend:    str
    threshold:  float
    ttl_seconds: int


# ── Health & observability ────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "ai-gateway", "version": "1.0.0"}


@app.get("/ready", tags=["ops"])
async def readiness():
    """Deep readiness — checks Redis connectivity."""
    try:
        stats = await cache.stats("CX")
        if stats.get("error"):
            raise HTTPException(status_code=503, detail="Redis unavailable")
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/metrics", tags=["ops"])
async def prometheus_metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Core pipeline (shared logic) ──────────────────────────────────────────────

async def _run_pipeline(request: ChatRequest, trace_id: str):
    """
    Runs the full 9-step pipeline and returns
    (sanitized_message, route_result, llm_response, latency_ms).
    Raises HTTPException on security or budget blocks.
    """
    start = time.monotonic()

    # Step 1a — Injection check
    injection = security.check_injection(request.message)
    if injection.blocked:
        metrics.injection_attempts.labels(department=request.department).inc()
        logger.warning("Injection blocked", extra={
            "trace_id": trace_id, "score": injection.score,
        })
        raise HTTPException(status_code=400, detail="Request blocked by security policy.")

    # Step 1b — PII masking
    sanitized, pii_fields = security.mask_pii(request.message)
    if pii_fields:
        metrics.pii_masked.labels(department=request.department).inc()
        logger.info("PII masked", extra={"trace_id": trace_id, "fields": pii_fields})

    metrics.requests_total.labels(department=request.department).inc()

    # Step 2 — Cache lookup
    cached = await cache.get(sanitized, request.department)
    if cached:
        latency_ms = (time.monotonic() - start) * 1000
        metrics.cache_hits.labels(department=request.department).inc()
        metrics.request_latency.labels(
            department=request.department, route="cache", model="none"
        ).observe(latency_ms / 1000)
        logger.info("Cache hit", extra={"trace_id": trace_id})
        return sanitized, None, cached, round(latency_ms, 2), True

    metrics.cache_misses.labels(department=request.department).inc()

    # Step 3 — Budget check
    budget_state = await budget.check(request.department)
    if budget_state.hard_limit_reached:
        raise HTTPException(status_code=429, detail="Department token budget exhausted.")

    # Step 4 — Semantic routing
    route_result = await semantic_router.route(sanitized, request.department, budget_state)
    metrics.route_decisions.labels(
        department=request.department, route=route_result.route
    ).inc()
    logger.info("Route decided", extra={
        "trace_id": trace_id,
        "route": route_result.route,
        "model": route_result.model,
        "reason": route_result.reason,
        "budget_downgraded": route_result.budget_downgraded,
    })

    return sanitized, route_result, None, time.monotonic() - start, False


async def _post_pipeline(
    department: str,
    route_result,
    response_text: str,
    tokens: int,
    sanitized_message: str,
    trace_id: str,
    start_monotonic: float,
):
    """Budget accounting, DLP scan, cache store, metrics, sentiment — after LLM call."""

    # Step 6 — DLP output scan
    clean_response, leaked = security.scan_output(response_text)
    if leaked:
        logger.warning("DLP: PII detected in model output — redacted", extra={
            "trace_id": trace_id, "fields": leaked,
        })

    # Step 7 — Budget record
    await budget.record(department, tokens)
    metrics.tokens_used.labels(
        department=department, model=route_result.model
    ).inc(tokens)

    # Step 8 — Cache store
    await cache.set(sanitized_message, department, {
        "response": clean_response,
        "route": route_result.route,
    })

    latency_ms = (time.monotonic() - start_monotonic) * 1000
    metrics.request_latency.labels(
        department=department,
        route=route_result.route,
        model=route_result.model,
    ).observe(latency_ms / 1000)

    # Step 9 — Sentiment
    sentiment = security.analyze_sentiment(sanitized_message)
    metrics.sentiment_score.labels(department=department).observe(sentiment)
    if sentiment < -0.5:
        logger.warning("Low sentiment — escalation candidate", extra={
            "trace_id": trace_id, "sentiment": sentiment,
        })

    return clean_response, round(latency_ms, 2)


# ── /v1/chat — standard response ─────────────────────────────────────────────

@app.post("/v1/chat", response_model=ChatResponse, tags=["chat"])
async def chat(request: ChatRequest):
    trace_id = str(uuid.uuid4())
    start    = time.monotonic()

    with RequestLogger(trace_id, request.department, request.message):
        sanitized, route_result, cached_payload, elapsed, cache_hit = \
            await _run_pipeline(request, trace_id)

        if cache_hit:
            return ChatResponse(
                trace_id=trace_id,
                response=cached_payload["response"],
                route=cached_payload["route"],
                model_used="cache",
                cache_hit=True,
                tokens_used=0,
                latency_ms=round(elapsed, 2),
            )

        # Step 5 — LLM call
        llm = await provider.complete(
            message=sanitized,
            system_prompt=route_result.system_prompt,
            model=route_result.model,
            trace_id=trace_id,
        )

        clean_response, latency_ms = await _post_pipeline(
            department=request.department,
            route_result=route_result,
            response_text=llm.text,
            tokens=llm.tokens_used,
            sanitized_message=sanitized,
            trace_id=trace_id,
            start_monotonic=start,
        )

        return ChatResponse(
            trace_id=trace_id,
            response=clean_response,
            route=route_result.route,
            model_used=route_result.model,
            cache_hit=False,
            tokens_used=llm.tokens_used,
            latency_ms=latency_ms,
        )


# ── /v1/chat/stream — SSE streaming response ─────────────────────────────────

@app.post("/v1/chat/stream", tags=["chat"])
async def chat_stream(request: ChatRequest):
    """
    Server-Sent Events streaming endpoint.
    Yields chunks as they arrive from Anthropic, then a final
    [DONE] event with trace_id and token count.
    """
    trace_id = str(uuid.uuid4())
    start    = time.monotonic()

    # Run security + cache + budget + routing synchronously first
    sanitized, route_result, cached_payload, elapsed, cache_hit = \
        await _run_pipeline(request, trace_id)

    if cache_hit:
        async def _cached_stream():
            text = cached_payload["response"]
            # Emit the cached response as a single chunk then DONE
            yield f"data: {text}\n\n"
            yield f"data: [DONE] trace_id={trace_id} tokens=0 cache=true\n\n"

        return StreamingResponse(_cached_stream(), media_type="text/event-stream")

    async def _live_stream():
        accumulated = ""
        tokens = 0
        async for chunk in provider.stream(
            message=sanitized,
            system_prompt=route_result.system_prompt,
            model=route_result.model,
            trace_id=trace_id,
        ):
            if chunk.startswith("\x00TOKENS:"):
                tokens = int(chunk.split(":")[1])
                continue
            accumulated += chunk
            yield f"data: {chunk}\n\n"

        # Post-pipeline: DLP, budget, cache, metrics, sentiment
        clean, latency_ms = await _post_pipeline(
            department=request.department,
            route_result=route_result,
            response_text=accumulated,
            tokens=tokens,
            sanitized_message=sanitized,
            trace_id=trace_id,
            start_monotonic=start,
        )
        yield f"data: [DONE] trace_id={trace_id} tokens={tokens} latency_ms={latency_ms}\n\n"

    return StreamingResponse(_live_stream(), media_type="text/event-stream")


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.get("/v1/budget", tags=["admin"])
async def get_budget():
    """Current token spend and burn rate per department."""
    return await budget.status()


@app.delete("/v1/cache/{department}", tags=["admin"])
async def invalidate_cache(department: str):
    """Flush the semantic cache for a given department."""
    if department not in ("CX", "IT", "FINANCE", "ALL"):
        raise HTTPException(status_code=400, detail="Unknown department.")
    if department == "ALL":
        counts = {}
        for dept in ("CX", "IT", "FINANCE"):
            counts[dept] = await cache.invalidate_department(dept)
        logger.info("Cache flushed for all departments", extra=counts)
        return {"invalidated": counts}
    count = await cache.invalidate_department(department)
    logger.info(f"Cache flushed for {department}", extra={"entries_removed": count})
    return {"department": department, "entries_removed": count}


@app.get("/v1/cache/{department}/stats", tags=["admin"])
async def cache_stats(department: str):
    """Cache entry count and embedding backend info for a department."""
    return await cache.stats(department)


# ── Global error handler ──────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception", exc_info=exc)
    metrics.errors_total.inc()
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal gateway error."},
    )
