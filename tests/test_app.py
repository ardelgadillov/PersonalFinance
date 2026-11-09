"""Web app: service functions behind the Settings page, and every route/tab renders."""
import base64
import datetime as dt

import pytest

from finance import db as dbmod
from finance import services
from finance.demo import build_demo
from finance.config import get_settings

TODAY = dt.date(2026, 11, 9)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    monkeypatch.delenv("SIMPLEFIN_ACCESS_URL", raising=False)
    info = build_demo(get_settings(), today=TODAY)
    yield info
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


def test_account_edits(demo):
    rows = {r["id"]: r for r in services.account_rows()}
    assert rows["DEMO-CHK"]["type"] == "checking" and rows["DEMO-CHK"]["transactions"] > 0
    services.update_account("DEMO-CHK", "display_name", "  Main checking ")
    services.update_account("DEMO-CHK", "hidden", True)
    services.update_account("DEMO-CC", "type", None)
    rows = {r["id"]: r for r in services.account_rows()}
    assert rows["DEMO-CHK"]["display_name"] == "Main checking" and rows["DEMO-CHK"]["hidden"]
    assert rows["DEMO-CC"]["type"] == "" and services.untyped_account_count() == 1
    with pytest.raises(ValueError):
        services.update_account("DEMO-CHK", "type", "spaceship")
    with pytest.raises(ValueError):
        services.update_account("DEMO-CHK", "balance", 1)


def test_upload_link_and_categories(demo):
    data = demo["csv_path"].read_bytes()
    payload = "data:text/csv;base64," + base64.b64encode(data).decode()
    res = services.import_empower_upload(payload, "../../evil name.csv")
    assert res.ok, res.message
    saved = list(get_settings().import_dir.glob("*evil_name.csv"))
    assert saved and saved[0].parent == get_settings().import_dir  # no path traversal

    rows, options = services.empower_link_rows()
    by_name = {r["empower_name"]: r for r in rows}
    ally = next(n for n in by_name if n.startswith("Ally"))
    assert by_name[ally]["linked_to"].startswith("Ally Bank")
    msg = services.set_empower_link(ally, "History only (not linked)")
    assert "Link saved" in msg
    rows, _ = services.empower_link_rows()
    assert {r["empower_name"]: r for r in rows}[ally]["linked_to"].startswith("History")
    assert "Auto-linked 1" in services.auto_link_empower()

    cats = services.category_rows()
    assert any(c["category"] == "Dining" and c["transactions"] > 0 for c in cats)
    assert not [r for r in services.empower_category_rows() if r["rows"] and not r["category"]]

    bad = services.import_empower_upload("data:text/csv;base64," + base64.b64encode(b"a,b\n1,2").decode(), "x.csv")
    assert not bad.ok and "Missing column" in bad.message


def test_all_routes_and_tabs_render(demo):
    from finance.app.main import create_app
    from finance.app.pages import placeholder, settings as settings_page

    app = create_app()
    assert app.layout is not None
    for path, args in placeholder.PAGES.items():
        assert placeholder.layout(*args)
    settings_page.render_tab("categories", 0)
    for tab in ("accounts", "empower", "categories", "synclog"):
        assert settings_page.render_tab(tab, 0)
    # account edit callback maps the display label back to the stored type
    msg = settings_page.save_account_edit([{"colId": "type", "value": "Credit card",
                                            "data": {"id": "DEMO-SAV", "name": "Online Savings"}}])
    assert "Saved" in str(msg)
    assert {r["id"]: r for r in services.account_rows()}["DEMO-SAV"]["type"] == "credit_card"


def test_humanize_age():
    now = dt.datetime(2026, 1, 2, 12)
    assert services.humanize_age(None) == "never"
    assert services.humanize_age(now - dt.timedelta(seconds=30), now) == "just now"
    assert services.humanize_age(now - dt.timedelta(hours=3), now) == "3 h ago"
    assert services.humanize_age(now - dt.timedelta(days=5), now) == "5 days ago"
    assert services.safe_filename("") == "upload.csv"
