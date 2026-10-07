"""FastAPI service: five REST endpoints over the rules engine and phrasers."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Response, status

from app.models import CategoryRules, GeneratedMessage, Merchant, MessagePlan, TriggerEvent
from app.phrasing import Phraser, TemplatePhraser, gemini_from_env, phrase_with_guardrail
from app.rules import MissingPayloadError, build_plan
from app.store import Store

app = FastAPI(
    title="AI Merchant Messaging Engine",
    version="1.0.0",
    description=(
        "Deterministic trigger -> merchant -> category rules decide what to say; "
        "Gemini (optional) only decides how to say it."
    ),
)

_store = Store.seeded()
_llm: Phraser | None = gemini_from_env()
_template = TemplatePhraser()


def get_store() -> Store:
    return _store


def get_llm() -> Phraser | None:
    return _llm


def _plan(event: TriggerEvent, store: Store) -> MessagePlan:
    merchant = store.get_merchant(event.merchant_id)
    if merchant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown merchant '{event.merchant_id}'")
    try:
        return build_plan(event, merchant, store.category_for(merchant))
    except MissingPayloadError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/health")
def health(llm: Phraser | None = Depends(get_llm), store: Store = Depends(get_store)) -> dict:
    return {
        "status": "ok",
        "phraser": llm.name if llm else "template",
        "merchants": len(store.merchants),
        "categories": len(store.categories),
    }


@app.post("/merchants", response_model=Merchant)
def ingest_merchant(merchant: Merchant, response: Response, store: Store = Depends(get_store)) -> Merchant:
    created = store.upsert_merchant(merchant)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return merchant


@app.post("/categories", response_model=CategoryRules)
def ingest_category(rules: CategoryRules, response: Response, store: Store = Depends(get_store)) -> CategoryRules:
    created = store.upsert_category(rules)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return store.categories[rules.category.lower()]


@app.post("/messages/plan", response_model=MessagePlan)
def plan_message(event: TriggerEvent, store: Store = Depends(get_store)) -> MessagePlan:
    """Run only the deterministic rules: no LLM call, same input gives same output."""
    return _plan(event, store)


@app.post("/messages/generate", response_model=GeneratedMessage)
def generate_message(
    event: TriggerEvent,
    store: Store = Depends(get_store),
    llm: Phraser | None = Depends(get_llm),
) -> GeneratedMessage:
    plan = _plan(event, store)
    if not plan.should_send:
        return GeneratedMessage(plan=plan, text=None)
    text, phraser, fallback_reason = phrase_with_guardrail(plan, llm, _template)
    return GeneratedMessage(plan=plan, text=text, phraser=phraser, fallback_reason=fallback_reason)
