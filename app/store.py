"""In-memory store for merchant and category context, seeded from data/*.json.

State lives in process memory, so anything ingested through the API is lost on
restart. That is deliberate for a small demo service; swap this class for a
database-backed one if the data must persist.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from app.models import CategoryRules, Merchant

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_CATEGORY = "default"


class Store:
    def __init__(self) -> None:
        self._lock = Lock()
        self.merchants: dict[str, Merchant] = {}
        self.categories: dict[str, CategoryRules] = {}

    @classmethod
    def seeded(cls, data_dir: Path = DATA_DIR) -> "Store":
        store = cls()
        for raw in json.loads((data_dir / "categories.json").read_text()):
            store.upsert_category(CategoryRules(**raw))
        for raw in json.loads((data_dir / "merchants.json").read_text()):
            store.upsert_merchant(Merchant(**raw))
        return store

    def upsert_merchant(self, merchant: Merchant) -> bool:
        """Insert or replace; returns True if the merchant was new."""
        with self._lock:
            created = merchant.merchant_id not in self.merchants
            self.merchants[merchant.merchant_id] = merchant
            return created

    def upsert_category(self, rules: CategoryRules) -> bool:
        with self._lock:
            key = rules.category.lower()
            created = key not in self.categories
            self.categories[key] = rules.model_copy(update={"category": key})
            return created

    def get_merchant(self, merchant_id: str) -> Merchant | None:
        return self.merchants.get(merchant_id)

    def category_for(self, merchant: Merchant) -> CategoryRules:
        """Rules for the merchant's category, or the default rules if unknown."""
        return self.categories.get(merchant.category.lower()) or self.categories[DEFAULT_CATEGORY]
