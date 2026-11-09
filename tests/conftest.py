from pathlib import Path

import pytest

FIXTURE_CATEGORIES = Path(__file__).parent / "fixtures" / "categories.csv"


@pytest.fixture(autouse=True)
def fixed_categories(monkeypatch):
    """Tests seed new databases from a frozen category file, independent of the app's default."""
    monkeypatch.setenv("FINANCE_CATEGORIES_SEED", str(FIXTURE_CATEGORIES))
