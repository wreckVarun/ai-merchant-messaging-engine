import json

import httpx
import pytest

from app.models import TriggerEvent, TriggerType
from app.phrasing import (
    GeminiPhraser,
    TemplatePhraser,
    _inr,
    build_prompt,
    phrase_with_guardrail,
    validate_text,
)
from app.rules import build_plan
from app.store import Store

SAMPLE_PAYLOADS = {
    TriggerType.SETTLEMENT_DELAYED: {"amount": 1234567, "expected_date": "9 Oct 2026"},
    TriggerType.PAYMENT_FAILURE_SPIKE: {"failure_rate": 0.32, "window_minutes": 15},
    TriggerType.SALES_DROP: {"drop_pct": 35},
    TriggerType.KYC_EXPIRING: {"days_left": 5},
    TriggerType.FESTIVE_SEASON: {"festival": "Diwali"},
    TriggerType.INACTIVE_MERCHANT: {"days_inactive": 21},
}


def settlement_plan(merchant, grocery):
    return build_plan(
        TriggerEvent(merchant_id="t_1", trigger=TriggerType.SETTLEMENT_DELAYED,
                     payload=SAMPLE_PAYLOADS[TriggerType.SETTLEMENT_DELAYED]),
        merchant, grocery,
    )


def test_inr_uses_indian_grouping():
    assert _inr(1234567) == "Rs 12,34,567"
    assert _inr(999) == "Rs 999"
    assert _inr(12500.4) == "Rs 12,500"


def all_sendable_plans():
    store = Store.seeded()
    for merchant in store.merchants.values():
        for trigger, payload in SAMPLE_PAYLOADS.items():
            plan = build_plan(TriggerEvent(merchant_id=merchant.merchant_id, trigger=trigger, payload=payload),
                              merchant, store.category_for(merchant))
            if plan.should_send:
                yield plan


@pytest.mark.parametrize("plan", list(all_sendable_plans()), ids=lambda p: f"{p.merchant_id}-{p.trigger.value}")
def test_every_template_passes_the_guardrail(plan):
    text = TemplatePhraser().phrase(plan)
    assert validate_text(plan, text) == []


def test_validator_flags_length_missing_fact_and_banned_term(merchant, grocery):
    plan = settlement_plan(merchant, grocery)
    problems = validate_text(plan, "Your money is guaranteed. " + "x" * 400)
    assert any("exceeds max_chars" in p for p in problems)
    assert any("missing required fact amount" in p for p in problems)
    assert any("banned term" in p for p in problems)


def test_prompt_contains_facts_and_constraints(merchant, grocery):
    prompt = build_prompt(settlement_plan(merchant, grocery))
    assert "Rs 12,34,567" in prompt and "9 Oct 2026" in prompt
    assert "guaranteed" in prompt
    assert "Do not add a reason or cause" in prompt


class FakeLLM:
    name = "gemini"

    def __init__(self, text):
        self.text = text

    def phrase(self, plan):
        return self.text


def test_good_llm_output_is_used(merchant, grocery):
    plan = settlement_plan(merchant, grocery)
    text, used, reason = phrase_with_guardrail(
        plan, FakeLLM("Asha, your Rs 12,34,567 settlement will arrive by 9 Oct 2026."), TemplatePhraser())
    assert used == "gemini" and reason is None


def test_bad_llm_output_falls_back_to_template(merchant, grocery):
    plan = settlement_plan(merchant, grocery)
    text, used, reason = phrase_with_guardrail(plan, FakeLLM("Your payout is guaranteed soon!"), TemplatePhraser())
    assert used == "template"
    assert "missing required fact" in reason
    assert "Rs 12,34,567" in text


def test_no_key_uses_template(merchant, grocery):
    _, used, reason = phrase_with_guardrail(settlement_plan(merchant, grocery), None, TemplatePhraser())
    assert used == "template" and reason == "GEMINI_API_KEY not set"


def _gemini_with(handler):
    return GeminiPhraser("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_gemini_phraser_parses_response(merchant, grocery):
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-goog-api-key"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": " Hello there \n"}]}}]})

    out = _gemini_with(handler).phrase(settlement_plan(merchant, grocery))
    assert out == "Hello there"
    assert seen["key"] == "test-key"
    assert "gemini-3.5-flash-lite:generateContent" in seen["url"]
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_gemini_http_error_falls_back(merchant, grocery):
    llm = _gemini_with(lambda r: httpx.Response(500, json={"error": "boom"}))
    _, used, reason = phrase_with_guardrail(settlement_plan(merchant, grocery), llm, TemplatePhraser())
    assert used == "template" and reason.startswith("Gemini call failed")
