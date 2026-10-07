def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["phraser"] == "template"
    assert r.json()["merchants"] == 4


def test_ingest_merchant_create_then_update(client):
    body = {"merchant_id": "m_9", "business_name": "Book Nook", "owner_name": "Ravi", "category": "books"}
    assert client.post("/merchants", json=body).status_code == 201
    assert client.post("/merchants", json={**body, "tier": "gold"}).status_code == 200


def test_ingest_category_then_used_in_plan(client):
    client.post("/merchants", json={"merchant_id": "m_9", "business_name": "Book Nook",
                                    "owner_name": "Ravi", "category": "books"})
    r = client.post("/categories", json={"category": "Books", "tone": "bookish",
                                         "allowed_offers": ["Free bookmarks"]})
    assert r.status_code == 201 and r.json()["category"] == "books"
    plan = client.post("/messages/plan", json={"merchant_id": "m_9", "trigger": "festive_season",
                                               "payload": {"festival": "Diwali"}}).json()
    assert plan["tone"] == "bookish" and plan["offer"] == "Free bookmarks"


def test_unknown_category_uses_default_rules(client):
    client.post("/merchants", json={"merchant_id": "m_9", "business_name": "Book Nook",
                                    "owner_name": "Ravi", "category": "books"})
    plan = client.post("/messages/plan", json={"merchant_id": "m_9", "trigger": "kyc_expiring",
                                               "payload": {"days_left": 10}}).json()
    assert "category:default:tone=friendly" in plan["rules_applied"]


def test_plan_unknown_merchant_404(client):
    r = client.post("/messages/plan", json={"merchant_id": "nope", "trigger": "kyc_expiring",
                                            "payload": {"days_left": 5}})
    assert r.status_code == 404


def test_plan_missing_payload_422(client):
    r = client.post("/messages/plan", json={"merchant_id": "m_1001", "trigger": "settlement_delayed",
                                            "payload": {}})
    assert r.status_code == 422 and "amount" in r.json()["detail"]


def test_generate_hindi_settlement(client):
    r = client.post("/messages/generate", json={"merchant_id": "m_1001", "trigger": "settlement_delayed",
                                                "payload": {"amount": 18250, "expected_date": "9 Oct 2026"}})
    body = r.json()
    assert r.status_code == 200
    assert body["phraser"] == "template"
    assert "Rs 18,250" in body["text"] and "नमस्ते" in body["text"]


def test_generate_suppressed_returns_no_text(client):
    r = client.post("/messages/generate", json={"merchant_id": "m_1003", "trigger": "sales_drop",
                                                "payload": {"drop_pct": 40}})
    body = r.json()
    assert body["plan"]["should_send"] is False and body["text"] is None


def test_generate_marketing_includes_disclaimer(client):
    body = client.post("/messages/generate", json={"merchant_id": "m_1004", "trigger": "festive_season",
                                                   "payload": {"festival": "Diwali"}}).json()
    assert body["text"].endswith("EMI subject to bank approval. T&C apply.")
