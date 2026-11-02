"""Where PrivatePartyNet finds its bundled files and where it keeps its own.

The same code runs from a source checkout and from the packaged build. PyInstaller unpacks bundled data under
sys._MEIPASS, so the lookup has to check for it.
"""

import os
import sys
from pathlib import Path

APP_FOLDER = "PrivatePartyNet"


def bundle_dir():
    """The folder that holds privatepartynet\\web and assets\\. The project root in a checkout, _MEIPASS in the build."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def web_dir():
    return bundle_dir() / "privatepartynet" / "web"


def asset_path(name):
    return bundle_dir() / "assets" / name


def data_dir():
    """Per-user folder for the certificate, settings and log. %LOCALAPPDATA%\\PrivatePartyNet on Windows."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_FOLDER
