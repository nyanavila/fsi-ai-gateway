import json
import logging
from dataclasses import dataclass

import anthropic

from .config import settings

logger = logging.getLogger(__name__)

# ── Route definitions ────────────────────────────────────────────────────────

ROUTES = {
    "CX_SIMPLE": {
        "model": settings.MODEL_SMALL,
        "system_prompt": (
            "You are a helpful customer support agent for a financial services company. "
            "Answer concisely and accurately. If you cannot resolve the issue, "
            "say so clearly and offer to escalate."
        ),
    },
    "CX_COMPLEX": {
        "model": settings.MODEL_LARGE,
        "system_prompt": (
            "You are a senior customer support specialist for a financial services company. "
            "The customer has a complex issue requiring careful reasoning. "
            "Be empathetic, thorough, and precise. Cite relevant policies where appropriate. "
            "Never speculate about account balances or transactions — always recommend "
            "the customer verify through secure channels."
        ),
    },
    "CX_ESCALATE": {
        "model": settings.MODEL_LARGE,
        "system_prompt": (
            "You are a customer support specialist handling a sensitive escalation for a "
            "financial services company. The customer is frustrated or distressed. "
            "Lead with empathy. Acknowledge the issue fully before attempting to resolve it. "
            "Do not be defensive. Offer concrete next steps and a named point of contact."
        ),
    },
    "IT_SIMPLE": {
        "model": settings.MODEL_SMALL,
        "system_prompt": (
            "You are an IT support assistant. Provide concise, step-by-step technical guidance."
        ),
    },
}

CLASSIFIER_PROMPT = """You are a routing classifier for an FSI (financial services) customer support AI gateway.

Classify the user query into exactly one of these routes:
- CX_SIMPLE: straightforward customer query (account info, product info, simple FAQs, password reset)
- CX_COMPLEX: requires reasoning or policy knowledge (disputes, refunds, complex transactions, regulatory questions)
- CX_ESCALATE: customer is angry, distressed, or the issue is time-sensitive / high-value
- IT_SIMPLE: internal IT issue (VPN, hardware, software, access)

Respond ONLY with valid JSON, no markdown:
{"route": "<ROUTE>", "reason": "<one sentence>", "confidence": <0.0-1.0>}"""


@dataclass
class RouteResult:
    route: str
    model: str
    system_prompt: str
    reason: str
    confidence: float
    budget_downgraded: bool = False


class SemanticRouter:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def route(self, message: str, department: str, budget_state) -> RouteResult:
        try:
            raw = self.client.messages.create(
                model=settings.MODEL_SMALL,
                max_tokens=128,
                system=CLASSIFIER_PROMPT,
                messages=[{"role": "user", "content": message}],
            )
            text = raw.content[0].text.strip()
            parsed = json.loads(text)
            route = parsed.get("route", "CX_SIMPLE")
            reason = parsed.get("reason", "")
            confidence = float(parsed.get("confidence", 0.8))
        except Exception as e:
            logger.warning(f"Classifier failed, defaulting to CX_SIMPLE: {e}")
            route = "CX_SIMPLE"
            reason = "classifier error — safe default"
            confidence = 0.0

        # Validate route exists
        if route not in ROUTES:
            route = "CX_SIMPLE"

        route_config = ROUTES[route]
        model = route_config["model"]
        budget_downgraded = False

        # Budget-triggered downgrade: if CX budget is over threshold,
        # downgrade non-escalation routes to the small model
        if budget_state.cx_over_threshold and route not in ("CX_ESCALATE",):
            model = settings.MODEL_SMALL
            budget_downgraded = True
            logger.info(f"Budget downgrade applied: {route} → {settings.MODEL_SMALL}")

        return RouteResult(
            route=route,
            model=model,
            system_prompt=route_config["system_prompt"],
            reason=reason,
            confidence=confidence,
            budget_downgraded=budget_downgraded,
        )
