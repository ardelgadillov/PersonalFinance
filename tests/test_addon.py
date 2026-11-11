"""Home Assistant add-on integration: option loading, ingress prefix, daily scheduler."""
import json

import pytest

from finance import addon
from finance import db as dbmod
from finance import services
from finance.background import sync_manager
from finance.config import get_settings
from finance.demo import build_demo


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_DEMO", "1")
    build_demo(get_settings())
    yield
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    services._initialized.clear()


def test_normalize_prefix():
    assert addon.normalize_prefix("/api/hassio_ingress/abc") == "/api/hassio_ingress/abc/"
    assert addon.normalize_prefix("api/hassio_ingress/abc/") == "/api/hassio_ingress/abc/"
    assert addon.normalize_prefix("") is None
    assert addon.normalize_prefix("/") is None
    assert addon.normalize_prefix(None) is None


def test_in_addon(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert addon.in_addon() is False
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    assert addon.in_addon() is True


def test_apply_options_sets_env(tmp_path, monkeypatch):
    for env in ("SIMPLEFIN_ACCESS_URL", "FINANCE_TIMEZONE", "FINANCE_DAILY_SYNC_HOUR"):
        monkeypatch.delenv(env, raising=False)
    opts = {"simplefin_access_url": "https://u:p@bridge/simplefin", "timezone": "Europe/Madrid",
            "daily_sync_hour": 4, "sync_max_age_hours": 6}
    path = tmp_path / "options.json"
    path.write_text(json.dumps(opts), "utf-8")

    returned = addon.apply_options(path)
    assert returned == opts
    import os
    assert os.environ["SIMPLEFIN_ACCESS_URL"] == "https://u:p@bridge/simplefin"
    assert os.environ["FINANCE_TIMEZONE"] == "Europe/Madrid"
    assert os.environ["FINANCE_DAILY_SYNC_HOUR"] == "4"  # coerced to str for the env


def test_apply_options_does_not_override_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "env-wins")
    (tmp_path / "options.json").write_text(json.dumps({"simplefin_access_url": "file-loses"}), "utf-8")
    addon.apply_options(tmp_path / "options.json")
    import os
    assert os.environ["SIMPLEFIN_ACCESS_URL"] == "env-wins"


def test_apply_options_missing_file_is_safe(tmp_path):
    assert addon.apply_options(tmp_path / "nope.json") == {}


def test_create_app_uses_ingress_prefix(demo, monkeypatch):
    from finance.app import main

    monkeypatch.setattr(addon, "in_addon", lambda: True)
    monkeypatch.setattr(addon, "ingress_prefix", lambda: "/api/hassio_ingress/tok/")
    app = main.create_app()
    assert app.config.requests_pathname_prefix == "/api/hassio_ingress/tok/"
    assert app.config.routes_pathname_prefix == "/"


def test_create_app_no_prefix_when_not_in_addon(demo, monkeypatch):
    from finance.app import main

    monkeypatch.setattr(addon, "in_addon", lambda: False)
    app = main.create_app()
    assert app.config.requests_pathname_prefix == "/"


def test_schedule_daily(demo):
    try:
        sched = sync_manager.schedule_daily(3, settings=get_settings())
        assert sched is not None
        jobs = sched.get_jobs()
        assert len(jobs) == 1 and jobs[0].id == "daily-sync"
    finally:
        if sync_manager._scheduler is not None:
            sync_manager._scheduler.shutdown(wait=False)
            sync_manager._scheduler = None


def test_schedule_daily_skipped_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINANCE_DEMO", raising=False)
    monkeypatch.delenv("SIMPLEFIN_ACCESS_URL", raising=False)
    assert sync_manager.schedule_daily(3, settings=get_settings()) is None
    assert sync_manager._scheduler is None
