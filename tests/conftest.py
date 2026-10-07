import pytest
from fastapi.testclient import TestClient

from app.main import app, get_llm, get_store
from app.models import CategoryRules, Merchant
from app.store import Store


@pytest.fixture
def store() -> Store:
    return Store.seeded()


@pytest.fixture
def client(store):
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def merchant() -> Merchant:
    return Merchant(
        merchant_id="t_1",
        business_name="Test Traders",
        owner_name="Asha",
        category="grocery",
        preferred_channel="whatsapp",
    )


@pytest.fixture
def grocery(store) -> CategoryRules:
    return store.categories["grocery"]
