"""Deterministic decision logic: trigger -> merchant -> category.

Nothing in this module calls an LLM. Given the same trigger, merchant and category
it always returns the same MessagePlan, which is what makes it unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.models import (
    CategoryRules,
    Channel,
    Merchant,
    MessagePlan,
    Priority,
    TriggerEvent,
    TriggerType,
)

SMS_MAX_CHARS = 160
PRIORITY_ORDER = [Priority.LOW, Priority.MEDIUM, Priority.HIGH, Priority.CRITICAL]
CHANNEL_FALLBACK_ORDER = [Channel.WHATSAPP, Channel.SMS, Channel.PUSH, Channel.EMAIL]


@dataclass(frozen=True)
class TriggerRule:
    intent: str
    marketing: bool
    required_payload: list[str]
    call_to_action: str
    # Returns (should_send, reason, priority) from the event payload.
    evaluate: Callable[[dict[str, Any]], tuple[bool, str, Priority]]
    needs_offer: bool = False
    extra_facts: list[str] = field(default_factory=list)


def _settlement(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    return True, "settlement is delayed", Priority.HIGH


def _failure_spike(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    rate = float(p["failure_rate"])
    if rate < 0.10:
        return False, f"failure rate {rate:.0%} is below the 10% alert threshold", Priority.LOW
    if rate >= 0.25:
        return True, f"failure rate {rate:.0%} is at or above 25%", Priority.CRITICAL
    return True, f"failure rate {rate:.0%} is between 10% and 25%", Priority.HIGH


def _sales_drop(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    drop = float(p["drop_pct"])
    if drop < 20:
        return False, f"sales drop of {drop:g}% is below the 20% threshold", Priority.LOW
    return True, f"sales dropped {drop:g}% week over week", Priority.MEDIUM


def _kyc(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    days = int(p["days_left"])
    if days > 30:
        return False, f"KYC expires in {days} days, outside the 30-day reminder window", Priority.LOW
    if days <= 3:
        return True, f"KYC expires in {days} days", Priority.CRITICAL
    if days <= 7:
        return True, f"KYC expires in {days} days", Priority.HIGH
    return True, f"KYC expires in {days} days", Priority.MEDIUM


def _festive(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    return True, f"{p['festival']} is coming up", Priority.LOW


def _inactive(p: dict[str, Any]) -> tuple[bool, str, Priority]:
    days = int(p["days_inactive"])
    if days < 14:
        return False, f"inactive for {days} days, below the 14-day threshold", Priority.LOW
    return True, f"no transactions for {days} days", Priority.LOW


TRIGGER_RULES: dict[TriggerType, TriggerRule] = {
    TriggerType.SETTLEMENT_DELAYED: TriggerRule(
        intent="inform_settlement_delay",
        marketing=False,
        required_payload=["amount", "expected_date"],
        call_to_action="Track the settlement status in the app",
        evaluate=_settlement,
    ),
    TriggerType.PAYMENT_FAILURE_SPIKE: TriggerRule(
        intent="alert_payment_failures",
        marketing=False,
        required_payload=["failure_rate", "window_minutes"],
        call_to_action="Check your payment terminal and internet connection",
        evaluate=_failure_spike,
    ),
    TriggerType.SALES_DROP: TriggerRule(
        intent="nudge_sales_recovery",
        marketing=True,
        required_payload=["drop_pct"],
        call_to_action="See tips to bring customers back in the app",
        evaluate=_sales_drop,
    ),
    TriggerType.KYC_EXPIRING: TriggerRule(
        intent="remind_kyc_renewal",
        marketing=False,
        required_payload=["days_left"],
        call_to_action="Renew your KYC in the app",
        evaluate=_kyc,
    ),
    TriggerType.FESTIVE_SEASON: TriggerRule(
        intent="promote_festive_offer",
        marketing=True,
        required_payload=["festival"],
        call_to_action="Activate the offer in the app",
        evaluate=_festive,
        needs_offer=True,
    ),
    TriggerType.INACTIVE_MERCHANT: TriggerRule(
        intent="reengage_merchant",
        marketing=True,
        required_payload=["days_inactive"],
        call_to_action="Open the app to restart accepting payments",
        evaluate=_inactive,
    ),
}


class MissingPayloadError(ValueError):
    """Raised when a trigger event lacks a field its rule requires."""


def _bump(priority: Priority) -> Priority:
    i = PRIORITY_ORDER.index(priority)
    return PRIORITY_ORDER[min(i + 1, len(PRIORITY_ORDER) - 1)]


def _pick_channel(merchant: Merchant, priority: Priority) -> Channel | None:
    blocked = set(merchant.opted_out_channels)
    # Critical alerts go by SMS first: it reaches feature phones and needs no data.
    order = [Channel.SMS] if priority == Priority.CRITICAL else []
    order += [merchant.preferred_channel] + CHANNEL_FALLBACK_ORDER
    for channel in order:
        if channel not in blocked:
            return channel
    return None


def build_plan(event: TriggerEvent, merchant: Merchant, category: CategoryRules) -> MessagePlan:
    """Run trigger -> merchant -> category rules and return a deterministic plan."""
    rule = TRIGGER_RULES[event.trigger]
    applied: list[str] = []

    def suppressed(reason: str) -> MessagePlan:
        return MessagePlan(
            merchant_id=merchant.merchant_id,
            trigger=event.trigger,
            should_send=False,
            reason=reason,
            intent=rule.intent,
            language=merchant.language,
            rules_applied=applied,
        )

    # --- 1. Trigger rules -------------------------------------------------
    missing = [k for k in rule.required_payload if k not in event.payload]
    if missing:
        raise MissingPayloadError(
            f"trigger '{event.trigger.value}' requires payload fields: {', '.join(missing)}"
        )
    send, reason, priority = rule.evaluate(event.payload)
    applied.append(f"trigger:{event.trigger.value}")
    if not send:
        return suppressed(reason)

    # --- 2. Merchant rules ------------------------------------------------
    if not merchant.active and event.trigger != TriggerType.KYC_EXPIRING:
        applied.append("merchant:inactive_account")
        return suppressed("merchant account is deactivated; only KYC reminders are sent")
    if rule.marketing and not merchant.marketing_opt_in:
        applied.append("merchant:marketing_opt_out")
        return suppressed("merchant has opted out of marketing messages")
    if merchant.tier == "platinum" and not rule.marketing:
        priority = _bump(priority)
        applied.append("merchant:platinum_priority_bump")
    channel = _pick_channel(merchant, priority)
    if channel is None:
        applied.append("merchant:all_channels_opted_out")
        return suppressed("merchant has opted out of every channel")
    applied.append(f"merchant:channel={channel.value}")

    # --- 3. Category rules ------------------------------------------------
    offer = None
    disclaimer = None
    if rule.marketing:
        offer = category.allowed_offers[0] if category.allowed_offers else None
        disclaimer = category.disclaimer
        if rule.needs_offer and offer is None:
            applied.append(f"category:{category.category}:no_offer")
            return suppressed(f"category '{category.category}' has no approved offer")
    max_chars = category.max_chars
    if channel == Channel.SMS:
        max_chars = min(max_chars, SMS_MAX_CHARS)
    applied.append(f"category:{category.category}:tone={category.tone}")

    facts: dict[str, Any] = {
        "business_name": merchant.business_name,
        "owner_name": merchant.owner_name,
    }
    facts.update({k: event.payload[k] for k in rule.required_payload})
    if offer:
        facts["offer"] = offer

    return MessagePlan(
        merchant_id=merchant.merchant_id,
        trigger=event.trigger,
        should_send=True,
        reason=reason,
        intent=rule.intent,
        priority=priority,
        channel=channel,
        language=merchant.language,
        tone=category.tone,
        max_chars=max_chars,
        facts=facts,
        required_facts=list(rule.required_payload),
        call_to_action=rule.call_to_action,
        offer=offer,
        disclaimer=disclaimer,
        banned_terms=list(category.banned_terms),
        rules_applied=applied,
    )
