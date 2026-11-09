"""Smoke-test every CLI command end to end against a demo database."""
import pytest

from finance import db as dbmod
from finance.cli import main


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SIMPLEFIN_ACCESS_URL", raising=False)
    yield tmp_path / "demo"
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()


def test_cli_commands(demo_env, capsys):
    assert main(["demo"]) == 0
    assert main(["demo"]) == 1  # refuses to overwrite without --reset
    csv_path = demo_env / "import" / "empower_transactions.csv"
    assert main(["--demo", "import-empower", str(csv_path)]) == 0
    assert main(["--demo", "empower-links"]) == 0
    assert main(["--demo", "categories"]) == 0
    assert main(["--demo", "sync"]) == 0
    assert main(["--demo", "status"]) == 0
    out = capsys.readouterr().out
    assert "Auto-linked Empower accounts" in out and "history only" in out
