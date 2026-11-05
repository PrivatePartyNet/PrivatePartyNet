"""The host window's remembered settings: port and your name, kept as JSON in the data folder."""

import json
from pathlib import Path

from privatepartynet.paths import data_dir

THEMES = ("system", "light", "dark")
DEFAULTS = {"port": 8765, "name": None, "theme": "dark", "server_id": None, "game_server": None, "game_open": []}  # name None means: use this PC's name; server_id is made on the first party
# Forwarding for other members who cannot reach each other (a friend relay, pathway.py): off until the person turns it
# on, since it uses their upload, and then never more than this many megabits a second for everyone together.
DEFAULTS.update({"friend_relay": False, "friend_relay_mbps": 8})
# How every window looks to this person: their own accent color (None is the default blue), text size in percent, and
# spacing. Each PC keeps its own; nothing here is sent to anyone.
DEFAULTS.update({"accent": None, "text_scale": 100, "density": "default"})
# Sounds (K, 2026-11-05): how loud (0 to 100), and which groups are off. Each PC keeps its own.
SOUND_GROUPS = ("voice", "mute", "messages", "requests")
DEFAULTS.update({"sound_volume": 60, "sounds_off": []})
DENSITIES = ("compact", "default", "spacious")
TEXT_SCALES = (90, 130)  # smallest and largest text size, in percent
MAX_FRIEND_MBPS = 1000
# Updates (updater.py): checked for and downloaded by themselves, test versions included, unless the person says not.
# update_skip is a version the person chose to skip.
DEFAULTS.update({"auto_update": True, "update_prerelease": True, "update_skip": ""})
SETTINGS_FILE = "settings.json"
MAX_SAVED_NAME = 200  # the window trims names properly; this only stops a damaged file from growing huge


def valid_port(port):
    return isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535


def valid_mbps(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_FRIEND_MBPS


def valid_accent(value):
    """A color as #rrggbb, or None for the default."""
    return value is None or (isinstance(value, str) and len(value) == 7 and value[0] == "#" and
                             all(c in "0123456789abcdef" for c in value[1:].lower()))


def valid_text_scale(value):
    return isinstance(value, int) and not isinstance(value, bool) and TEXT_SCALES[0] <= value <= TEXT_SCALES[1]


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
        opened = saved.get("game_open")
        if isinstance(opened, list):  # Windows services party members may reach on this PC (gamenet.SERVICE_GROUPS)
            settings["game_open"] = sorted({g for g in opened if isinstance(g, str) and g.isidentifier() and len(g) <= 32})
        if isinstance(saved.get("name"), str) and saved["name"].strip():
            settings["name"] = saved["name"][:MAX_SAVED_NAME]
        if isinstance(saved.get("friend_relay"), bool):
            settings["friend_relay"] = saved["friend_relay"]
        if valid_mbps(saved.get("friend_relay_mbps")):
            settings["friend_relay_mbps"] = saved["friend_relay_mbps"]
        if "accent" in saved and valid_accent(saved["accent"]):
            settings["accent"] = saved["accent"].lower() if saved["accent"] else None
        if valid_text_scale(saved.get("text_scale")):
            settings["text_scale"] = saved["text_scale"]
        if saved.get("density") in DENSITIES:
            settings["density"] = saved["density"]
        volume = saved.get("sound_volume")
        if isinstance(volume, int) and not isinstance(volume, bool) and 0 <= volume <= 100:
            settings["sound_volume"] = volume
        if isinstance(saved.get("sounds_off"), list):
            settings["sounds_off"] = sorted({g for g in saved["sounds_off"] if g in SOUND_GROUPS})
        for flag in ("auto_update", "update_prerelease"):
            if isinstance(saved.get(flag), bool):
                settings[flag] = saved[flag]
        skip = saved.get("update_skip")
        if isinstance(skip, str) and len(skip) <= 40 and all(c.isalnum() or c in ".-+" for c in skip):
            settings["update_skip"] = skip
    return settings


def save_settings(settings, folder=None):
    path = settings_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
