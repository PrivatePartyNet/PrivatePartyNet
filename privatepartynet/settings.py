"""The host window's remembered settings: port and your name, kept as JSON in the data folder."""

import json
from pathlib import Path

from privatepartynet.paths import data_dir

THEMES = ("system", "light", "dark")
DEFAULTS = {"port": 8765, "name": None, "theme": "system", "server_id": None, "game_server": None}  # name None means: use this PC's name; server_id is made on the first party
SETTINGS_FILE = "settings.json"
MAX_SAVED_NAME = 200  # the window trims names properly; this only stops a damaged file from growing huge


def valid_port(port):
    return isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535


def settings_path(folder=None):
    return Path(folder or data_dir()) / SETTINGS_FILE


def load_settings(folder=None):
    """Return the saved settings with defaults filled in. A missing or damaged file gives the defaults."""
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(settings_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        if valid_port(saved.get("port")):
            settings["port"] = saved["port"]
        if saved.get("theme") in THEMES:
            settings["theme"] = saved["theme"]
        server_id = saved.get("server_id")
        if isinstance(server_id, str) and 8 <= len(server_id) <= 32 and all(c in "0123456789abcdef" for c in server_id):
            settings["server_id"] = server_id
        game = saved.get("game_server")
        if isinstance(game, str) and (game == "own" or game.startswith("joined-")) and len(game) <= 80:
            settings["game_server"] = game
        if isinstance(saved.get("name"), str) and saved["name"].strip():
            settings["name"] = saved["name"][:MAX_SAVED_NAME]
    return settings


def save_settings(settings, folder=None):
    path = settings_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
