# AI Merchant Messaging Engine

**Live API:** https://ai-merchant-messaging-engine.onrender.com/docs (Render free plan, so the first request after a while asleep takes ~1 minute)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/wreckVarun/ai-merchant-messaging-engine)

A FastAPI service that turns business events (a delayed settlement, a spike in failed payments, an expiring KYC, a festival) into short notifications for small merchants.

The design rule is simple: **code decides what to say, the LLM only decides how to say it.**

```
TriggerEvent ──► trigger rules ──► merchant rules ──► category rules ──► MessagePlan
                 (send? priority)   (opt-outs, tier,   (tone, offer,      (deterministic,
                                     channel, language) disclaimer, limits) unit-tested)
                                                                               │
                                                     ┌─────────────────────────┤
                                                     ▼                         ▼
                                              Gemini phraser            Template phraser
                                                     │                         ▲
                                                     └──► guardrail ── fail ───┘
                                                          (length, required facts,
                                                           banned terms)
```

Whether a message is sent, to which channel, at what priority, and which numbers, dates and offers it contains are all decided by plain Python in `app/rules.py`. Gemini receives that plan and writes the sentence. Its output is then checked: if it is too long, drops a required fact (like the rupee amount), or uses a banned word for that category, the service discards it and uses a fixed template instead. So the output is always controlled and testable, and the service works with no API key at all.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Status, which phraser is active (`gemini` or `template`), counts of loaded merchants and categories |
| POST | `/merchants` | Ingest merchant context (category, language, tier, preferred channel, opt-outs). 201 on create, 200 on update |
| POST | `/categories` | Ingest category rules (tone, max length, approved offers, banned terms, disclaimer) |
| POST | `/messages/plan` | Run only the deterministic rules for a trigger and return the plan. No LLM call |
| POST | `/messages/generate` | Plan, then phrase with Gemini (or template) behind the guardrail |

Interactive docs are at `/docs` once the server is running.

### Triggers

| Trigger | Required payload | Rule |
|---|---|---|
| `settlement_delayed` | `amount`, `expected_date` | Always sent, high priority |
| `payment_failure_spike` | `failure_rate` (0–1), `window_minutes` | Below 10%: not sent. 10–25%: high. 25%+: critical, goes by SMS |
| `sales_drop` | `drop_pct` | Sent at 20%+ drop. Marketing |
| `kyc_expiring` | `days_left` | Sent within 30 days. ≤7 high, ≤3 critical. Sent even to deactivated accounts |
| `festive_season` | `festival` | Marketing. Not sent if the category has no approved offer |
| `inactive_merchant` | `days_inactive` | Sent at 14+ days. Marketing |

Merchant rules: marketing is suppressed for merchants who opted out; deactivated accounts only get KYC reminders; platinum merchants get transactional alerts one priority level higher; opted-out channels are skipped, and SMS messages are capped at 160 characters. Unknown categories fall back to the `default` category rules.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

To use Gemini for phrasing, set a key first (get one at https://aistudio.google.com/apikey):

```bash
export GEMINI_API_KEY=your-key
export GEMINI_MODEL=gemini-3.8-flash   # optional, this is the default
```

### Example

```bash
curl -X POST localhost:8000/messages/generate \
  -H 'content-type: application/json' \
  -d '{"merchant_id":"m_1002","trigger":"payment_failure_spike",
       "payload":{"failure_rate":0.31,"window_minutes":15}}'
```

```json
{
  "plan": {
    "should_send": true,
    "reason": "failure rate 31% is at or above 25%",
    "priority": "critical",
    "channel": "sms",
    "max_chars": 160,
    "rules_applied": ["trigger:payment_failure_spike", "merchant:channel=sms", "category:restaurant:tone=upbeat"],
    "...": "..."
  },
  "text": "Alert for Spice Route Cafe: 31% of payments failed in the last 15 min. Check your payment terminal and internet connection.",
  "phraser": "template",
  "fallback_reason": "GEMINI_API_KEY not set"
}
```

A Hindi-speaking merchant (`m_1001`) gets Hindi output, for example:
`नमस्ते Ramesh Sharma, आपका KYC 2 दिनों में समाप्त होगा। ऐप में अपना KYC रिन्यू करें।`

## Tests

```bash
pytest -q
```

60 tests cover every trigger threshold, each merchant and category rule, determinism of the plan, the guardrail (length, missing facts, banned terms), the fallback path when Gemini errors or returns bad text, the Gemini request/response handling (with a mocked HTTP transport, so no key or network needed), and all five endpoints. A parametrized test also checks that every template, for every seeded merchant and trigger, passes the same guardrail the LLM output must pass.

## Deploy to Render

`render.yaml` is a Render Blueprint. Click the **Deploy to Render** button at the top, or in Render choose **New > Blueprint** and pick this repo. Set `GEMINI_API_KEY` in the service's environment when prompted (or leave it empty to run on templates). Render uses `/health` as the health check.

## Project layout

```
app/
  main.py      FastAPI app and the five endpoints
  rules.py     trigger -> merchant -> category rules (no LLM)
  phrasing.py  Gemini phraser, template phraser, guardrail
  models.py    Pydantic schemas
  store.py     in-memory store seeded from data/
data/          seed merchants and category rules
tests/         pytest suite
render.yaml    Render Blueprint
```

## Limitations

- Merchant and category data live in memory, seeded from `data/*.json`. Anything posted to `/merchants` or `/categories` is lost on restart. Swap `Store` for a database-backed class if that matters.
- The seed merchants are made up for the demo.
- On Render's free plan the service sleeps when idle, so the first request after a pause is slow.
- Messages are generated, not delivered; sending them over SMS or WhatsApp would need a provider such as Twilio or Gupshup.
