import json, logging, re
from dataclasses import dataclass
import anthropic
from .config import settings

logger = logging.getLogger(__name__)

ROUTES = {
    "CX_SIMPLE":   {"model": settings.MODEL_SMALL,  "system_prompt": "You are a helpful customer support agent for a financial services company. Answer concisely and accurately."},
    "CX_COMPLEX":  {"model": settings.MODEL_MEDIUM, "system_prompt": "You are a senior customer support specialist for a financial services company. Be empathetic, thorough, and precise. Never speculate about account balances — recommend customers verify through secure channels."},
    "CX_ESCALATE": {"model": settings.MODEL_LARGE,  "system_prompt": "You are handling a sensitive escalation for a financial services customer. Lead with empathy. Acknowledge the issue fully before resolving. Offer concrete next steps."},
    "IT_SIMPLE":   {"model": settings.MODEL_SMALL,  "system_prompt": "You are an IT support assistant. Provide concise, step-by-step technical guidance."},
    "IT_COMPLEX":  {"model": settings.MODEL_MEDIUM, "system_prompt": "You are a senior IT support engineer. Diagnose and resolve complex technical issues methodically."},
}

CLASSIFIER_PROMPT = """You are a routing classifier for a financial services customer support gateway.

Classify the user query into exactly one route:

- CX_ESCALATE: ALWAYS use when the customer uses ANY of:
  * angry words: furious, outraged, disgusted, livid, unacceptable
  * accusations: stolen, fraud, scam, lied, cheated, incompetent
  * demands: manager, supervisor, lawyer, legal action, complaint
  * urgency: NOW, immediately, urgent, emergency
  * threats: sue, report, regulator, ombudsman, FCA, CFPB

- CX_COMPLEX: requires policy knowledge or reasoning
  (disputes, refunds, complex transactions, regulatory questions)

- CX_SIMPLE: straightforward query
  (account info, FAQs, product info, simple requests)

- IT_COMPLEX: complex technical issue
  (network outages, security incidents, system failures, data loss)

- IT_SIMPLE: basic internal IT issue
  (VPN, password reset, hardware, software installs, access)

When in doubt between CX_SIMPLE and CX_ESCALATE, always choose CX_ESCALATE.

Respond ONLY with valid JSON, no markdown fences:
{"route":"<ROUTE>","reason":"<one sentence>","confidence":<0.0-1.0>}"""

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

    async def route(self, message, department, budget_state):
        try:
            raw = self.client.messages.create(
                model=settings.MODEL_SMALL, max_tokens=128,
                system=CLASSIFIER_PROMPT,
                messages=[{"role": "user", "content": message}])
            text = raw.content[0].text.strip()

            # Strip markdown fences if present
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

            parsed = json.loads(text)
            route = parsed.get("route", "CX_SIMPLE")
            reason = parsed.get("reason", "")
            confidence = float(parsed.get("confidence", 0.8))
            logger.info("Route classified", extra={
                "route": route, "reason": reason, "confidence": confidence,
            })
        except Exception as e:
            logger.warning(f"Classifier failed: {e}")
            route, reason, confidence = "CX_SIMPLE", "classifier error — safe default", 0.0

        if route not in ROUTES:
            route = "CX_SIMPLE"

        route_config = ROUTES[route]
        model = route_config["model"]
        budget_downgraded = False

        if budget_state.cx_over_threshold and route != "CX_ESCALATE":
            model = settings.MODEL_SMALL
            budget_downgraded = True
            logger.info(f"Budget downgrade: {route} -> {settings.MODEL_SMALL}")

        return RouteResult(route=route, model=model,
            system_prompt=route_config["system_prompt"],
            reason=reason, confidence=confidence,
            budget_downgraded=budget_downgraded)
