"""Load the project's JSON config files (settings, rules, watchlist, blocklist)."""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(rel_path):
    with open(REPO_ROOT / rel_path, encoding="utf-8") as f:
        return json.load(f)


def load_settings():
    return _load("config/settings.json")


def load_rules():
    return _load("playbook/rules.json")


def load_watchlist():
    return _load("config/watchlist.json")


def load_blocklist():
    return _load("config/blocklist.json")
