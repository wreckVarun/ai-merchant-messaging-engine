import pytest

from app.models import Channel, Priority, TriggerEvent, TriggerType
from app.rules import MissingPayloadError, build_plan


def ev(trigger, **payload):
    return TriggerEvent(merchant_id="t_1", trigger=trigger, payload=payload)


# --- trigger rules ---------------------------------------------------------

@pytest.mark.parametrize(
    "rate,send,priority",
    [(0.05, False, None), (0.10, True, Priority.HIGH), (0.24, True, Priority.HIGH), (0.25, True, Priority.CRITICAL)],
)
def test_failure_spike_thresholds(merchant, grocery, rate, send, priority):
    plan = build_plan(ev(TriggerType.PAYMENT_FAILURE_SPIKE, failure_rate=rate, window_minutes=15), merchant, grocery)
    assert plan.should_send is send
    assert plan.priority == priority


@pytest.mark.parametrize("days,send,priority", [(31, False, None), (30, True, Priority.MEDIUM), (7, True, Priority.HIGH), (3, True, Priority.CRITICAL)])
def test_kyc_window(merchant, grocery, days, send, priority):
    plan = build_plan(ev(TriggerType.KYC_EXPIRING, days_left=days), merchant, grocery)
    assert plan.should_send is send
    assert plan.priority == priority


def test_sales_drop_below_threshold_is_suppressed(merchant, grocery):
    plan = build_plan(ev(TriggerType.SALES_DROP, drop_pct=15), merchant, grocery)
    assert not plan.should_send
    assert "below the 20% threshold" in plan.reason


def test_inactive_merchant_threshold(merchant, grocery):
    assert not build_plan(ev(TriggerType.INACTIVE_MERCHANT, days_inactive=13), merchant, grocery).should_send
    assert build_plan(ev(TriggerType.INACTIVE_MERCHANT, days_inactive=14), merchant, grocery).should_send


def test_missing_payload_raises(merchant, grocery):
    with pytest.raises(MissingPayloadError, match="expected_date"):
        build_plan(ev(TriggerType.SETTLEMENT_DELAYED, amount=5000), merchant, grocery)


# --- merchant rules --------------------------------------------------------

def test_marketing_opt_out_suppresses_marketing_only(merchant, grocery):
    m = merchant.model_copy(update={"marketing_opt_in": False})
    assert not build_plan(ev(TriggerType.FESTIVE_SEASON, festival="Diwali"), m, grocery).should_send
    assert build_plan(ev(TriggerType.SETTLEMENT_DELAYED, amount=1, expected_date="9 Oct"), m, grocery).should_send


def test_deactivated_merchant_only_gets_kyc(merchant, grocery):
    m = merchant.model_copy(update={"active": False})
    assert not build_plan(ev(TriggerType.SETTLEMENT_DELAYED, amount=1, expected_date="9 Oct"), m, grocery).should_send
    assert build_plan(ev(TriggerType.KYC_EXPIRING, days_left=5), m, grocery).should_send


def test_platinum_bumps_transactional_priority(merchant, grocery):
    m = merchant.model_copy(update={"tier": "platinum"})
    plan = build_plan(ev(TriggerType.SETTLEMENT_DELAYED, amount=1, expected_date="9 Oct"), m, grocery)
    assert plan.priority == Priority.CRITICAL
    assert "merchant:platinum_priority_bump" in plan.rules_applied


def test_platinum_does_not_bump_marketing(merchant, grocery):
    m = merchant.model_copy(update={"tier": "platinum"})
    plan = build_plan(ev(TriggerType.SALES_DROP, drop_pct=40), m, grocery)
    assert plan.priority == Priority.MEDIUM


def test_critical_goes_to_sms_and_caps_length(merchant, grocery):
    plan = build_plan(ev(TriggerType.PAYMENT_FAILURE_SPIKE, failure_rate=0.4, window_minutes=10), merchant, grocery)
    assert plan.channel == Channel.SMS
    assert plan.max_chars == 160


def test_opted_out_channel_is_skipped(merchant, grocery):
    m = merchant.model_copy(update={"opted_out_channels": [Channel.WHATSAPP]})
    plan = build_plan(ev(TriggerType.KYC_EXPIRING, days_left=20), m, grocery)
    assert plan.channel == Channel.SMS


def test_all_channels_opted_out(merchant, grocery):
    m = merchant.model_copy(update={"opted_out_channels": list(Channel)})
    plan = build_plan(ev(TriggerType.KYC_EXPIRING, days_left=20), m, grocery)
    assert not plan.should_send


# --- category rules --------------------------------------------------------

def test_category_supplies_tone_offer_and_disclaimer(merchant, grocery):
    plan = build_plan(ev(TriggerType.FESTIVE_SEASON, festival="Diwali"), merchant, grocery)
    assert plan.tone == grocery.tone
    assert plan.offer == grocery.allowed_offers[0]
    assert plan.disclaimer == "T&C apply."
    assert plan.banned_terms == grocery.banned_terms


def test_festive_without_offer_is_suppressed(merchant, store):
    plan = build_plan(ev(TriggerType.FESTIVE_SEASON, festival="Diwali"), merchant, store.categories["pharmacy"])
    assert not plan.should_send
    assert "no approved offer" in plan.reason


def test_transactional_has_no_offer_or_disclaimer(merchant, grocery):
    plan = build_plan(ev(TriggerType.SETTLEMENT_DELAYED, amount=1, expected_date="9 Oct"), merchant, grocery)
    assert plan.offer is None and plan.disclaimer is None


def test_plan_is_deterministic(merchant, grocery):
    e = ev(TriggerType.SALES_DROP, drop_pct=33)
    assert build_plan(e, merchant, grocery) == build_plan(e, merchant, grocery)
