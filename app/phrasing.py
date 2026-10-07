"""Turn a MessagePlan into text.

Two phrasers share one guardrail:
- TemplatePhraser: fixed templates, always available, fully deterministic.
- GeminiPhraser: asks the Gemini API to word the plan. It never decides *whether*
  to send, *who* to send to or *which facts* to state; those come from the plan.

Every Gemini output is checked by `validate_text`. If it is too long, drops a
required fact, or uses a banned term, the service falls back to the template.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from app.models import MessagePlan, TriggerType

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Fact formatting (shared by templates, the Gemini prompt and the validator)
# ---------------------------------------------------------------------------

def _inr(value: Any) -> str:
    """Format a number as Indian rupees with Indian digit grouping (12,34,567)."""
    n = int(round(float(value)))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + "Rs " + s


def format_fact(key: str, value: Any) -> str:
    if key == "amount":
        return _inr(value)
    if key == "failure_rate":
        return f"{float(value):.0%}"
    if key == "drop_pct":
        return f"{float(value):g}%"
    return str(value)


def display_facts(plan: MessagePlan) -> dict[str, str]:
    return {k: format_fact(k, v) for k, v in plan.facts.items()}


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------

def body_budget(plan: MessagePlan) -> int:
    """Characters available for the body once the disclaimer is appended."""
    if plan.disclaimer:
        return plan.max_chars - len(plan.disclaimer) - 1
    return plan.max_chars


def finalize(plan: MessagePlan, body: str) -> str:
    body = " ".join(body.split())
    return f"{body} {plan.disclaimer}" if plan.disclaimer else body


def validate_text(plan: MessagePlan, text: str) -> list[str]:
    """Return a list of problems with `text`; empty means it may be sent."""
    problems: list[str] = []
    if not text.strip():
        problems.append("empty text")
    if len(text) > plan.max_chars:
        problems.append(f"length {len(text)} exceeds max_chars {plan.max_chars}")
    facts = display_facts(plan)
    for key in plan.required_facts:
        if facts[key] not in text:
            problems.append(f"missing required fact {key}={facts[key]!r}")
    lowered = text.lower()
    for term in plan.banned_terms:
        if term.lower() in lowered:
            problems.append(f"contains banned term {term!r}")
    return problems


# ---------------------------------------------------------------------------
# Template phraser
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, dict[TriggerType, str]] = {
    "en": {
        TriggerType.SETTLEMENT_DELAYED: "Hi {owner_name}, your settlement of {amount} is delayed and is now expected by {expected_date}.",
        TriggerType.PAYMENT_FAILURE_SPIKE: "Alert for {business_name}: {failure_rate} of payments failed in the last {window_minutes} min.",
        TriggerType.SALES_DROP: "Hi {owner_name}, sales at {business_name} are down {drop_pct} this week.",
        TriggerType.KYC_EXPIRING: "Hi {owner_name}, your KYC expires in {days_left} days.",
        TriggerType.FESTIVE_SEASON: "{festival} is coming! Offer for {business_name}: {offer}.",
        TriggerType.INACTIVE_MERCHANT: "Hi {owner_name}, we have not seen a payment at {business_name} in {days_inactive} days.",
    },
    "hi": {
        TriggerType.SETTLEMENT_DELAYED: "नमस्ते {owner_name}, आपका {amount} का सेटलमेंट देर से होगा, अब {expected_date} तक अपेक्षित है।",
        TriggerType.PAYMENT_FAILURE_SPIKE: "{business_name} के लिए अलर्ट: पिछले {window_minutes} मिनट में {failure_rate} पेमेंट फेल हुए।",
        TriggerType.SALES_DROP: "नमस्ते {owner_name}, इस हफ्ते {business_name} की बिक्री {drop_pct} कम हुई है।",
        TriggerType.KYC_EXPIRING: "नमस्ते {owner_name}, आपका KYC {days_left} दिनों में समाप्त होगा।",
        TriggerType.FESTIVE_SEASON: "{festival} आ रहा है! {business_name} के लिए ऑफर: {offer}।",
        TriggerType.INACTIVE_MERCHANT: "नमस्ते {owner_name}, {business_name} पर {days_inactive} दिनों से कोई पेमेंट नहीं आया।",
    },
}


# Hindi wording of each rule's call to action, keyed by intent.
CTA_HI: dict[str, str] = {
    "inform_settlement_delay": "ऐप में सेटलमेंट की स्थिति देखें",
    "alert_payment_failures": "अपना पेमेंट टर्मिनल और इंटरनेट कनेक्शन जांचें",
    "nudge_sales_recovery": "ग्राहकों को वापस लाने के टिप्स ऐप में देखें",
    "remind_kyc_renewal": "ऐप में अपना KYC रिन्यू करें",
    "promote_festive_offer": "ऐप में ऑफर एक्टिवेट करें",
    "reengage_merchant": "पेमेंट लेना फिर से शुरू करने के लिए ऐप खोलें",
}


class Phraser(Protocol):
    name: str

    def phrase(self, plan: MessagePlan) -> str: ...


class TemplatePhraser:
    name = "template"

    def phrase(self, plan: MessagePlan) -> str:
        templates = TEMPLATES.get(plan.language, TEMPLATES["en"])
        core = templates[plan.trigger].format(**display_facts(plan))
        budget = body_budget(plan)
        cta = plan.call_to_action
        if cta and plan.language == "hi":
            cta = CTA_HI.get(plan.intent or "", cta)
        end = "।" if plan.language == "hi" else "."
        with_cta = f"{core} {cta}{end}" if cta else core
        body = with_cta if len(with_cta) <= budget else core
        if len(body) > budget:
            body = body[: budget - 1].rstrip() + "…"
        return finalize(plan, body)


# ---------------------------------------------------------------------------
# Gemini phraser
# ---------------------------------------------------------------------------

class PhraserError(RuntimeError):
    pass


def build_prompt(plan: MessagePlan) -> str:
    facts = display_facts(plan)
    lines = [
        "Write one merchant notification message.",
        f"Language: {'Hindi (Devanagari)' if plan.language == 'hi' else 'English'}",
        f"Channel: {plan.channel.value if plan.channel else 'unknown'}",
        f"Purpose: {plan.intent}",
        f"Tone: {plan.tone}",
        f"Maximum length: {body_budget(plan)} characters, including spaces.",
        "Facts (do not invent any other numbers, dates, offers or names):",
        *[f"- {k}: {v}" for k, v in facts.items()],
        "These values must appear exactly as written: "
        + ", ".join(repr(facts[k]) for k in plan.required_facts),
    ]
    if plan.call_to_action:
        lines.append(f"End with this call to action, reworded if needed: {plan.call_to_action}")
    if plan.banned_terms:
        lines.append("Never use these words: " + ", ".join(plan.banned_terms))
    lines.append("Return only the message text, no quotes, no emojis, no markdown.")
    return "\n".join(lines)


class GeminiPhraser:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.Client(timeout=timeout)

    def phrase(self, plan: MessagePlan) -> str:
        body = {
            "systemInstruction": {
                "parts": [{"text": "You write short, accurate notifications for small-business merchants of a payments company."}]
            },
            "contents": [{"role": "user", "parts": [{"text": build_prompt(plan)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
        }
        try:
            resp = self.client.post(
                GEMINI_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise PhraserError(f"Gemini call failed: {exc}") from exc
        return finalize(plan, text.strip().strip('"'))


def gemini_from_env() -> GeminiPhraser | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    return GeminiPhraser(key, model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))


def phrase_with_guardrail(
    plan: MessagePlan, llm: Phraser | None, fallback: Phraser
) -> tuple[str, str, str | None]:
    """Return (text, phraser_name, fallback_reason)."""
    if llm is None:
        return fallback.phrase(plan), fallback.name, "GEMINI_API_KEY not set"
    try:
        text = llm.phrase(plan)
    except PhraserError as exc:
        return fallback.phrase(plan), fallback.name, str(exc)
    problems = validate_text(plan, text)
    if problems:
        return fallback.phrase(plan), fallback.name, "; ".join(problems)
    return text, llm.name, None
