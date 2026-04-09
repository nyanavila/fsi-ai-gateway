import logging
import json
import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, REGISTRY


# ── Prometheus metrics ────────────────────────────────────────────────────────

class GatewayMetrics:
    def __init__(self):
        self.requests_total = Counter(
            "gateway_requests_total",
            "Total requests received",
            ["department"],
        )
        self.cache_hits = Counter(
            "gateway_cache_hits_total",
            "Semantic cache hits",
            ["department"],
        )
        self.cache_misses = Counter(
            "gateway_cache_misses_total",
            "Semantic cache misses",
            ["department"],
        )
        self.tokens_used = Counter(
            "gateway_tokens_used_total",
            "Total tokens consumed",
            ["department", "model"],
        )
        self.route_decisions = Counter(
            "gateway_route_decisions_total",
            "Routing decisions by route type",
            ["department", "route"],
        )
        self.injection_attempts = Counter(
            "gateway_injection_attempts_total",
            "Blocked prompt injection attempts",
            ["department"],
        )
        self.pii_masked = Counter(
            "gateway_pii_masked_total",
            "Requests where PII was masked",
            ["department"],
        )
        self.errors_total = Counter(
            "gateway_errors_total",
            "Unhandled gateway errors",
        )
        self.request_latency = Histogram(
            "gateway_request_latency_seconds",
            "End-to-end request latency",
            ["department", "route", "model"],
            buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
        )
        self.sentiment_score = Histogram(
            "gateway_sentiment_score",
            "Customer sentiment scores (-1 to 1)",
            ["department"],
            buckets=[-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0],
        )
        self.budget_fraction = Gauge(
            "gateway_budget_fraction_used",
            "Fraction of daily token budget consumed",
            ["department"],
        )


metrics = GatewayMetrics()


# ── Structured JSON logging ───────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge any extra fields passed via `extra={...}`
        for key, value in record.__dict__.items():
            if key not in (
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "id", "levelname", "levelno",
                "lineno", "module", "msecs", "message", "msg", "name",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName",
            ):
                log_obj[key] = value
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


# ── Per-request context logger ────────────────────────────────────────────────

class RequestLogger:
    """
    Context manager that logs request start/end with trace_id.
    Usage:
        with RequestLogger(trace_id, department, message):
            ...
    """

    def __init__(self, trace_id: str, department: str, message: str):
        self.trace_id = trace_id
        self.department = department
        self.message_preview = message[:80]
        self.start = time.time()
        self.logger = logging.getLogger("gateway.request")

    def __enter__(self):
        self.logger.info("Request received", extra={
            "trace_id": self.trace_id,
            "department": self.department,
            "message_preview": self.message_preview,
        })
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = round((time.time() - self.start) * 1000, 1)
        if exc_type:
            self.logger.error("Request failed", extra={
                "trace_id": self.trace_id,
                "duration_ms": duration_ms,
                "error": str(exc_val),
            })
        else:
            self.logger.info("Request completed", extra={
                "trace_id": self.trace_id,
                "duration_ms": duration_ms,
            })
        return False   # do not suppress exceptions
