"""Command line: `stormify <command>` (or `python -m stormify <command>`)."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from importlib import resources
from pathlib import Path

from werkzeug.security import generate_password_hash

from .config import load_config
from .db import Database
from .delivery import DeliveryError, Notification, channels_for_user
from .engine import Engine
from .health import health
from .rules import RuleError, load_rules
from .sources.nws import parse_collection


def _db(cfg) -> Database:
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return Database(cfg.db_path)


def _user(db: Database, name: str) -> dict:
    u = db.get_user(name=name)
    if not u:
        sys.exit(f"No user named {name!r}. Create one with: stormify user add {name}")
    return u


def _password(given: str | None) -> str:
    if given:
        return given
    while True:
        p1 = getpass.getpass("Password: ")
        p2 = getpass.getpass("Again: ")
        if p1 and p1 == p2:
            return p1
        print("Passwords didn't match (or were empty). Try again.")


def example_rules() -> list[dict]:
    text = resources.files("stormify").joinpath("examples/rules.json").read_text()
    return json.loads(text)["rules"]


def _read_rules_file(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        sys.exit("Rules file must be a list or {\"rules\": [...]}")
    try:
        load_rules(rules)
    except (RuleError, TypeError) as e:
        sys.exit(f"Invalid rules: {e}")
    return rules


# ---- commands -------------------------------------------------------------

def cmd_init(cfg, args) -> int:
    db = _db(cfg)
    if db.get_user(name=args.user):
        print(f"User {args.user!r} already exists; database is ready at {cfg.db_path}")
        return 0
    uid = db.create_user(args.user, generate_password_hash(_password(args.password)),
                         {"timezone": args.timezone, "time_display": ["local", "event", "zulu"]})
    if not args.no_examples:
        db.replace_rules(uid, example_rules())
    print(f"Created user {args.user!r} in {cfg.db_path}"
          + ("" if args.no_examples else " with example rules"))
    print("Next: stormify ntfy --user", args.user, "--topic <your-topic> [--server http://localhost:2586]")
    return 0


def cmd_user(cfg, args) -> int:
    db = _db(cfg)
    if args.action == "add":
        db.create_user(args.name, generate_password_hash(_password(args.password)),
                       {"timezone": args.timezone, "time_display": ["local", "event", "zulu"]})
        print(f"Created user {args.name!r}")
    elif args.action == "passwd":
        u = _user(db, args.name)
        db.set_password(u["id"], generate_password_hash(_password(args.password)))
        print("Password updated")
    elif args.action == "list":
        for u in db.list_users():
            print(u["id"], u["name"])
    return 0


def cmd_rules(cfg, args) -> int:
    db = _db(cfg)
    u = _user(db, args.user)
    if args.action == "export":
        rules = db.list_rules(u["id"])
        for r in rules:
            r.pop("id", None)
        text = json.dumps({"stormify_rules": 1, "rules": rules}, indent=2)
        if args.file:
            Path(args.file).write_text(text + "\n")
            print(f"Wrote {len(rules)} rules to {args.file}")
        else:
            print(text)
    elif args.action == "import":
        if not args.file:
            sys.exit("rules import needs a file")
        rules = _read_rules_file(args.file)
        db.replace_rules(u["id"], rules)
        print(f"Imported {len(rules)} rules for {u['name']} (replaced existing)")
    elif args.action == "list":
        for r in db.list_rules(u["id"]):
            flag = "on " if r["enabled"] else "off"
            print(f"[{flag}] {r.get('action', 'push'):4}  {r['name']}")
    return 0


def cmd_ntfy(cfg, args) -> int:
    db = _db(cfg)
    u = _user(db, args.user)
    s = u["settings"]
    s.setdefault("channels", {})["ntfy"] = {
        "server": args.server, "topic": args.topic, "token": args.token, "enabled": True}
    db.update_settings(u["id"], s)
    print(f"ntfy set: {args.server}/{args.topic}")
    return 0


def cmd_test_push(cfg, args) -> int:
    db = _db(cfg)
    u = _user(db, args.user)
    channels = channels_for_user(u["settings"])
    if not channels:
        sys.exit("No delivery channel configured. Run: stormify ntfy --user NAME --topic TOPIC")
    n = Notification(title=args.title, body=args.body, priority=args.priority, is_test=True)
    for ch in channels:
        try:
            ch.send(n)
            print(f"Sent test via {ch.name}")
        except DeliveryError as e:
            print(f"{ch.name} failed: {e}")
            return 1
    return 0


def cmd_replay(cfg, args) -> int:
    """Run a saved NWS GeoJSON file through a user's rules.

    Uses a throwaway in-memory database so replays never touch the real archive.
    Default is a dry run that prints what would have pushed; --send delivers for real.
    """
    real = _db(cfg)
    u = _user(real, args.user)
    data = json.loads(Path(args.file).read_text())
    alerts = parse_collection(data)
    if args.send:
        for a in alerts:
            if "test" not in a.tags:
                a.tags.append("test")

    mem = Database(":memory:")
    uid = mem.create_user(u["name"], None, u["settings"])
    mem.replace_rules(uid, real.list_rules(u["id"]))
    cfg.max_push_per_poll = args.limit or cfg.max_push_per_poll
    res = Engine(mem, cfg).process(alerts, dry_run=not args.send, quiet=False)

    print(f"{len(alerts)} alerts replayed: {res.pushed} would push, {res.logged} log only"
          if not args.send else f"{len(alerts)} alerts replayed: {res.pushed} pushed, {res.logged} logged")
    for _, n in res.notifications:
        print(f"\n[P{n.priority}] {n.title}\n    " + n.body.replace("\n", "\n    "))
    if args.verbose:
        print("\nLogged:")
        for row in mem.query_feed(uid, limit=5000, action="log"):
            print(f"  - {row['event']} · {row['office']}: {row['reason']}")
    return 0


def cmd_poll(cfg, args) -> int:
    from .poller import Poller
    db = _db(cfg)
    p = Poller(cfg, db)
    if args.once:
        p.poll_once()
        print(json.dumps(health(db, cfg.heartbeat_stale_seconds), indent=2))
    else:
        p.run()
    return 0


def cmd_web(cfg, args) -> int:
    from waitress import serve

    from .web import create_app
    db = _db(cfg)
    app = create_app(cfg, db)
    host = args.host or cfg.web_host
    port = args.port or cfg.web_port
    print(f"Stormify dashboard on http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)
    return 0


def cmd_health(cfg, args) -> int:
    h = health(_db(cfg), cfg.heartbeat_stale_seconds)
    print(json.dumps(h, indent=2))
    return 0 if h["status"] == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stormify", description="Alerts-forward weather notifications")
    ap.add_argument("-c", "--config", help="path to config.toml (default: $STORMIFY_CONFIG or ./config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the database and your user, with example rules")
    p.add_argument("--user", required=True)
    p.add_argument("--password")
    p.add_argument("--timezone", default="America/Denver")
    p.add_argument("--no-examples", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("user", help="manage users")
    p.add_argument("action", choices=["add", "passwd", "list"])
    p.add_argument("name", nargs="?")
    p.add_argument("--password")
    p.add_argument("--timezone", default="America/Denver")
    p.set_defaults(fn=cmd_user)

    p = sub.add_parser("rules", help="list / export / import a user's rules (JSON)")
    p.add_argument("action", choices=["list", "export", "import"])
    p.add_argument("file", nargs="?")
    p.add_argument("--user", required=True)
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("ntfy", help="set a user's ntfy server and topic")
    p.add_argument("--user", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--server", default="http://localhost:2586")
    p.add_argument("--token")
    p.set_defaults(fn=cmd_ntfy)

    p = sub.add_parser("test-push", help="send a test notification")
    p.add_argument("--user", required=True)
    p.add_argument("--title", default="TEST Tornado Warning · BOU")
    p.add_argument("--body", default='2" hail · Arapahoe, Douglas\nStormify test notification')
    p.add_argument("--priority", type=int, default=4)
    p.set_defaults(fn=cmd_test_push)

    p = sub.add_parser("replay", help="run a saved NWS alerts JSON file through your rules")
    p.add_argument("file")
    p.add_argument("--user", required=True)
    p.add_argument("--send", action="store_true", help="actually push (marked TEST) instead of a dry run")
    p.add_argument("--limit", type=int, help="per-poll push cap to apply")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("poll", help="run the poller (forever, or --once)")
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("web", help="run the dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("health", help="print heartbeat status (exit 1 if stale)")
    p.set_defaults(fn=cmd_health)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.cmd == "user" and args.action in ("add", "passwd") and not args.name:
        ap.error("user add/passwd needs a name")
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
