"""Command line interface: python -m finance <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance.config import get_settings
from finance.db import init_db
from finance.models import Account, SyncLog, Transaction


def cmd_init_db(_: argparse.Namespace) -> int:
    init_db()
    print(f"Database ready at {get_settings().db_path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from finance.sync import run_sync

    result = run_sync(full=args.full, max_requests=args.max_requests)
    print(f"Sync {result.status} ({result.mode}): {result.requests} request(s), "
          f"{result.accounts} account(s), {result.transactions_new} new / "
          f"{result.transactions_updated} updated transaction(s)")
    for err in result.errors:
        print(f"  ! {err}")
    return 0 if result.status in ("ok", "partial") else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from finance.demo import build_demo

    settings = get_settings()
    try:
        info = build_demo(settings, reset=args.reset)
    except FileExistsError as exc:
        print(exc)
        return 1
    r = info["sync"]
    print(f"Demo database: {info['db_path']}")
    print(f"  fake SimpleFIN sync {r.status}: {r.requests} request(s), {r.accounts} account(s), "
          f"{r.transactions_new} transaction(s)")
    print(f"  365 days of balance and holdings snapshots")
    print(f"  fake Empower export: {info['csv_path']} ({info['csv_rows']} rows)")
    print("Next: python -m finance --demo status")
    return 0


def _print_links(sugs) -> None:
    for sg in sugs:
        target = sg.account_label or "-"
        print(f"  {sg.empower_name[:45]:<45} -> {target[:40]:<40} "
              f"{sg.matches:>4} matching txns{'  (confident)' if sg.confident else ''}")


def cmd_import_empower(args: argparse.Namespace) -> int:
    from finance.importers.empower import EmpowerImportError, import_empower_file

    engine = init_db()
    with Session(engine) as s:
        try:
            r = import_empower_file(s, args.file, do_auto_link=not args.no_auto_link)
        except (EmpowerImportError, FileNotFoundError) as exc:
            print(f"Import failed: {exc}")
            return 1
        s.commit()
        print(f"Read {r.rows_read} rows from {r.file} (skipped {r.rows_skipped}); "
              f"date format {r.date_format}; amounts {'sign-flipped' if r.flipped_sign else 'as-is'}")
        print(f"Staged: {r.staged_new} new, {r.staged_updated} updated, {r.staged_removed} removed")
        if r.linked:
            print("Auto-linked Empower accounts:")
            _print_links(r.linked)
        _print_link_status(s)
        if r.refresh:
            a = r.refresh.apply
            print(f"Transactions from Empower: {a.inserted} added, {a.updated} updated, {a.removed} removed; "
                  f"{a.matched} matched SimpleFIN transactions (categories copied), "
                  f"{a.covered_by_simplefin} skipped (covered by SimpleFIN)")
            print(f"Rules learned: {r.refresh.rules_learned}; transactions categorized by rules: "
                  f"{r.refresh.rules_applied}")
        if r.unmapped:
            print("Empower categories without a mapping (map them in Settings -> Categories):")
            for name, n in r.unmapped:
                print(f"  {name} ({n})")
    return 0


def _print_link_status(s: Session) -> None:
    from finance.importers.empower import _rows_by_account, history_account_id

    rows = _rows_by_account(s)
    links = {a.linked_empower_name: a for a in s.scalars(select(Account).where(
        Account.linked_empower_name.is_not(None)))}
    print("Empower account links:")
    for name in sorted(rows):
        a = links.get(name)
        target = f"{a.org_name or ''} {a.label} [{a.id}]" if a else f"history only [{history_account_id(name)}]"
        print(f"  {name[:45]:<45} -> {target}")


def cmd_empower_links(args: argparse.Namespace) -> int:
    from finance.importers.empower import auto_link, link_account, refresh_empower, suggest_links

    engine = init_db()
    with Session(engine) as s:
        if args.set:
            name, account_id = args.set
            link_account(s, name, None if account_id.lower() == "none" else account_id)
        elif args.auto:
            done = auto_link(s)
            print(f"Auto-linked {len(done)} account(s)")
        if args.set or args.auto:
            refresh_empower(s)
            s.commit()
        _print_link_status(s)
        print()
        print("Suggestions (by matching transactions):")
        _print_links(suggest_links(s))
        print()
        print("SimpleFIN account ids:")
        for a in s.scalars(select(Account).where(Account.source == "simplefin")):
            print(f"  {a.id:<30} {a.org_name or ''} {a.label}")
    return 0


def cmd_categories(_: argparse.Namespace) -> int:
    from finance import services

    print(f"{'Group':<9} {'Parent':<14} {'Category':<26} {'Txns':>6}  Empower categories")
    for c in services.category_rows():
        print(f"{c['group']:<9} {c['parent'][:14]:<14} {c['category'][:26]:<26} {c['transactions']:>6}  "
              f"{c['empower_categories']}")
    print(f"{'':<9} {'':<14} {'(uncategorized)':<26} {services.uncategorized_count():>6}")
    unmapped = [r for r in services.empower_category_rows() if not r["category"] and r["rows"]]
    if unmapped:
        print("Empower categories in your data with no app category:")
        for r in unmapped:
            print(f"  {r['empower_category']} ({r['rows']} rows)")
    print("Manage categories in the app: Settings -> Categories")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from finance import addon
    from finance.app.main import run

    if addon.in_addon():  # Home Assistant add-on: read /data/options.json into the environment
        addon.apply_options()
    run(host=args.host, port=args.port, debug=args.debug, startup_sync=not args.no_sync)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    engine = init_db()
    with Session(engine) as s:
        last = s.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(1)).first()
        if last:
            print(f"Last sync: {last.finished_at or last.started_at} UTC - {last.status} ({last.mode})")
            if last.errors_json:
                for err in json.loads(last.errors_json):
                    print(f"  ! {err}")
        else:
            print("No sync yet. Run: python -m finance sync")

        counts = dict(s.execute(select(Transaction.account_id, func.count())
                                .group_by(Transaction.account_id)).all())
        first = dict(s.execute(select(Transaction.account_id, func.min(Transaction.date))
                               .group_by(Transaction.account_id)).all())
        accounts = s.scalars(select(Account).order_by(Account.org_name, Account.name)).all()
        if accounts:
            print(f"\n{'Institution':<25} {'Account':<35} {'Type':<12} {'Balance':>14} {'Txns':>6}  Since")
            for a in accounts:
                bal = f"{a.balance:,.2f}" if a.balance is not None else "-"
                print(f"{(a.org_name or '')[:25]:<25} {a.label[:35]:<35} {(a.type or '?'):<12} "
                      f"{bal:>14} {counts.get(a.id, 0):>6}  {first.get(a.id, '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finance")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="Use fake data in data/demo instead of your real data and SimpleFIN")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the database and default categories").set_defaults(func=cmd_init_db)

    p = sub.add_parser("sync", help="Download data from SimpleFIN")
    p.add_argument("--full", action="store_true", help="Re-walk history in 45-day windows")
    p.add_argument("--max-requests", type=int, default=12,
                   help="Max SimpleFIN requests for a full sync (Bridge allows ~24/day)")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="Show accounts and last sync").set_defaults(func=cmd_status)

    p = sub.add_parser("import-empower", help="Import an Empower transactions CSV export")
    p.add_argument("file")
    p.add_argument("--no-auto-link", action="store_true", help="Do not auto-link accounts")
    p.set_defaults(func=cmd_import_empower)

    p = sub.add_parser("empower-links", help="Show / change Empower -> SimpleFIN account links")
    p.add_argument("--auto", action="store_true", help="Auto-link confident matches")
    p.add_argument("--set", nargs=2, metavar=("EMPOWER_NAME", "ACCOUNT_ID|none"))
    p.set_defaults(func=cmd_empower_links)

    sub.add_parser("categories", help="List categories (manage them in the app)")         .set_defaults(func=cmd_categories)

    p = sub.add_parser("app", help="Start the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--debug", action="store_true", help="Dash debug mode (error overlay, dev tools)")
    p.add_argument("--no-sync", action="store_true", help="Do not sync on startup")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("demo", help="Build the fake demo database (implies --demo)")
    p.add_argument("--reset", action="store_true", help="Delete and rebuild data/demo")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if args.demo or args.command == "demo":
        os.environ["FINANCE_DEMO"] = "1"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    from finance.categories import CategoryFileError
    try:
        return args.func(args)
    except CategoryFileError as exc:
        print(f"Error in categories file: {exc}")
        return 2
