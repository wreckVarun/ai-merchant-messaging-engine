"""Request/response schemas shared by the API, the rules engine and the phrasers."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TriggerType(str, Enum):
    SETTLEMENT_DELAYED = "settlement_delayed"
    PAYMENT_FAILURE_SPIKE = "payment_failure_spike"
    SALES_DROP = "sales_drop"
    KYC_EXPIRING = "kyc_expiring"
    FESTIVE_SEASON = "festive_season"
    INACTIVE_MERCHANT = "inactive_merchant"


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    PUSH = "push"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Merchant(BaseModel):
    """Merchant context ingested via POST /merchants."""

    merchant_id: str = Field(..., min_length=1)
    business_name: str = Field(..., min_length=1)
    owner_name: str = Field(..., min_length=1)
    category: str = Field(..., description="Must match a known category, e.g. 'grocery'")
    city: str | None = None
    language: str = Field("en", description="'en' or 'hi'")
    tier: str = Field("standard", description="'standard' | 'gold' | 'platinum'")
    preferred_channel: Channel = Channel.WHATSAPP
    opted_out_channels: list[Channel] = Field(default_factory=list)
    marketing_opt_in: bool = True
    active: bool = True


class CategoryRules(BaseModel):
    """Category context ingested via POST /categories."""

    category: str = Field(..., min_length=1)
    tone: str = Field("friendly", description="e.g. 'friendly', 'formal', 'reassuring'")
    max_chars: int = Field(320, ge=60, le=1000)
    allowed_offers: list[str] = Field(default_factory=list)
    banned_terms: list[str] = Field(default_factory=list)
    disclaimer: str | None = None
    peak_hours: str | None = Field(None, description="Informational, e.g. '18:00-22:00'")


class TriggerEvent(BaseModel):
    """A business event that may produce a message."""

    merchant_id: str
    trigger: TriggerType
    payload: dict[str, Any] = Field(default_factory=dict)


class MessagePlan(BaseModel):
    """Deterministic output of the rules engine. The phraser only turns this into words."""

    merchant_id: str
    trigger: TriggerType
    should_send: bool
    reason: str
    intent: str | None = None
    priority: Priority | None = None
    channel: Channel | None = None
    language: str = "en"
    tone: str = "friendly"
    max_chars: int = 320
    facts: dict[str, Any] = Field(default_factory=dict)
    required_facts: list[str] = Field(default_factory=list)
    call_to_action: str | None = None
    offer: str | None = None
    disclaimer: str | None = None
    banned_terms: list[str] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)


class GeneratedMessage(BaseModel):
    plan: MessagePlan
    text: str | None
    phraser: str | None = Field(None, description="'gemini' or 'template'")
    fallback_reason: str | None = None
