"""Updates: finding a newer PrivatePartyNet on GitHub, checking that it really is one, and installing it.

What it does, when automatic updates are on (the default): shortly after the app starts, and every six hours, it asks
GitHub's public API for the releases of PrivatePartyNet/PrivatePartyNet (test versions too, unless that is turned
off). A newer one is downloaded in the background and checked, and then the control panel offers it: Restart now, or
Later, which installs it when the app is next closed. With automatic updates off, nothing is asked of GitHub until
Check for updates is pressed; a newer version then offers Update, Later or Skip this version.

What is installed: the newest release of PrivatePartyNet/PrivatePartyNet on GitHub that is newer than the one running
(never older: no rollback), whoever published it there (K, 2026-11-05: "Any release on GitHub"; 3.2.0 also asked for
a signature from one release key, and 3.3.0 dropped that). Its installer or portable zip must carry the name the build
gives it, and the download must have the size and SHA-256 that GitHub itself publishes for that file (the asset's
"digest", which GitHub works out from the file it keeps); a release without them is not offered. Only HTTPS is used,
with Windows' certificates, and only GitHub's own hosts, which no setting, environment variable or command line can
change. The file is hashed again just before it runs, and held so that nothing can change it until it has started.
What this rests on is the repository: anyone who can publish a release there can update every copy.

Installing: an installed copy runs the checked installer for this user, silently with its progress showing, and the
installer starts the app again. A portable copy unpacks the checked zip beside its own folder and swaps the two once
the app has closed (the user's data is in %LOCALAPPDATA%, not in the app's folder, so it stays as it is). Nothing is
installed during a party, a call or while the game network is on; the control panel says it is waiting.
"""

import contextlib
import email.message
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from privatepartynet import __version__

log = logging.getLogger("privatepartynet.updater")

APP = "PrivatePartyNet"
REPOSITORY = "PrivatePartyNet/PrivatePartyNet"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
API_HOSTS = frozenset({"api.github.com"})
ASSET_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
FIRST_CHECK = (60.0, 180.0)  # seconds after the app starts, picked at random, so not every copy asks at once
CHECK_SECONDS = 6 * 3600
MAX_BACKOFF = 24 * 3600
MAX_LISTING = 2 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_FILE = 2 * 1024 ** 3
MAX_NOTES = 4000
KINDS = ("installer", "portable")
STAGING_MARK = ".privatepartynet-update"  # in each <app>.update-<version> folder the updater makes, so it cleans only those
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class UpdateError(Exception):
    """Why an update was refused or could not be fetched, in plain words."""


# --- versions -----------------------------------------------------------------------------------------------------


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$", re.ASCII)  # 0-9 only


class Version:
    """A version as Semantic Versioning orders it: 3.2.0-alpha.1 < 3.2.0-alpha.2 < 3.2.0-beta < 3.2.0 < 3.2.1. A
    leading "v" (a tag) is allowed; build metadata after "+" is ignored."""

    __slots__ = ("text", "core", "pre")

    def __init__(self, text):
        match = _VERSION.match(str(text).strip())
        if not match:
            raise ValueError(f"not a version: {text!r}")
        self.text = str(text).strip().lstrip("v")
        self.core = tuple(int(part) for part in match.group(1, 2, 3))
        self.pre = tuple(match.group(4).split(".")) if match.group(4) else ()

    @property
    def prerelease(self):
        return bool(self.pre)

    def _key(self):
        # A release sorts after every pre-release of it; numeric identifiers sort below alphanumeric ones.
        pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return self.core, (0, pre) if self.pre else (1, ())

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self == other or self < other

    def __gt__(self, other):
        return other < self

    def __hash__(self):
        return hash(self._key())

    def __str__(self):
        return self.text

    __repr__ = __str__


def parse_version(text):
    try:
        return Version(text)
    except ValueError:
        return None


def ready_message(version, waiting=""):
    """What the panel says of a checked update. Nothing restarts the app by itself: Restart now does, or closing it
    installs the update (and while `waiting`, a party, a call or the game network, Restart now waits for it to end)."""
    if waiting:
        return (f"{APP} {version} is ready. Restart now works once {waiting} ends, or it installs when you close "
                f"{APP}.")
    return f"Ready: Restart now, or {APP} {version} installs when you close {APP}."


def file_names(version):
    return {"installer": f"{APP}Setup-{version}.exe", "portable": f"{APP}-{version}-portable.zip"}


# --- checking a release ----------------------------------------------------------------------------------------------


def release_files(release, current):
    """What `release` (a GitHub release record) offers, when it is newer than `current`: {"version", "tag", "files":
    {kind: {"name", "size", "sha256"}}, "notes", "prerelease"}; else UpdateError. A file counts only under the name the
    build gives it, and only with the size and SHA-256 GitHub publishes for it."""
    tag = str(release.get("tag_name") or "")
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"{tag!r} is not a version")
    if not version > Version(current):
        raise UpdateError(f"{version} is not newer than {current}; an update never goes back")
    files = {}
    for kind, name in file_names(version).items():
        item = asset(release, name)
        if item is None:
            continue
        size = _size(item.get("size"))
        digest = re.fullmatch(r"sha256:([0-9a-f]{64})", str(item.get("digest") or "").lower())
        if not 0 < size <= MAX_FILE or digest is None:
            raise UpdateError(f"GitHub gives no size or SHA-256 for {name}")
        files[kind] = {"name": name, "size": size, "sha256": digest.group(1)}
    if not files:
        raise UpdateError("the release has no installer and no portable copy")
    return {"version": version, "tag": tag, "files": files, "notes": plain_notes(release.get("body")),
            "prerelease": bool(release.get("prerelease"))}


def plain_notes(text):
    """Release notes as plain text (never HTML), cut to MAX_NOTES characters."""
    text = "".join(ch for ch in str(text or "") if ch.isprintable() or ch in "\n\t")
    return text[:MAX_NOTES] + ("..." if len(text) > MAX_NOTES else "")


def candidates(releases, current, include_test, skipped=""):
    """The releases worth looking at, newest first: not drafts, test versions only when wanted, newer than `current`
    and not the version the person chose to skip."""
    current = Version(current)
    skip = parse_version(skipped) if skipped else None
    found = []
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        version = parse_version(release.get("tag_name"))
        if version is None or not version > current or (skip is not None and version == skip):
            continue
        if (release.get("prerelease") or version.prerelease) and not include_test:
            continue
        found.append((version, release))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [release for _, release in found]


def asset(release, name):
    for item in _list(release.get("assets")):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _list(value):
    """`value` when it is a list, else an empty one: GitHub's answer is not trusted to have the shape it should."""
    return value if isinstance(value, list) else []


def _size(value):
    """A size from GitHub's answer, or -1 when it is not a whole number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


# --- the network -----------------------------------------------------------------------------------------------------


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to HTTPS on one of the hosts allowed."""

    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, request, fp, code, message, headers, url):
        if not _allowed(url, self.hosts):
            raise UpdateError(f"the download was sent somewhere it may not come from ({urllib.parse.urlsplit(url).hostname})")
        return super().redirect_request(request, fp, code, message, headers, url)


def _allowed(url, hosts):
    parts = urllib.parse.urlsplit(str(url))
    return parts.scheme == "https" and (parts.hostname or "").lower() in hosts


class Fetcher:
    """HTTPS GETs with Windows' certificates (Python's default context loads the system store), redirects only to
    allowed hosts, and a size limit."""

    def __init__(self, context=None, timeout=30.0):
        self.context = context or ssl.create_default_context()
        self.timeout = timeout

    def get(self, url, hosts, headers=None, limit=MAX_MANIFEST, into=None, progress=None):
        """(status, headers, body). The headers are looked up whatever their case (GitHub sends some in lower case).
        With `into` (a file object), the body is written there instead and body is the number of bytes; `progress`, if
        given, is called with the bytes so far. Raises UpdateError on a host that is not allowed or a body over
        `limit`."""
        if not _allowed(url, hosts):
            raise UpdateError(f"{urllib.parse.urlsplit(str(url)).hostname} is not a host updates come from")
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.context), _Redirects(hosts))
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{__version__}", **(headers or {})})
        try:
            response = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error.code, error.headers or email.message.Message(), b""
        except UpdateError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise UpdateError(f"GitHub could not be reached ({error})") from None
        with response:
            total = 0
            chunks = []
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpdateError("a download was larger than it may be")
                if into is not None:
                    into.write(chunk)
                    if progress is not None:
                        progress(total)
                else:
                    chunks.append(chunk)
            return response.status, response.headers, total if into is not None else b"".join(chunks)


# --- where this copy runs ------------------------------------------------------------------------------------------


# The installer's record of itself (installer\PrivatePartyNet.iss: AppId, and Inno Setup's own "_is1" key), for this user.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{02FC5BBE-A733-4ACF-AE03-9CDDF2637230}_is1"


def install_kind(executable=None, frozen=None, location=None):
    """"installed" (by the installer), "portable" (unzipped anywhere) or "source" (a checkout, which git updates).
    Installed means the installer's record for this user names this copy's own folder as where it installed; an
    uninstaller file in the folder proves nothing. The arguments are for tests."""
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return "source"
    installed = (location or _installed_location)()
    if not installed:
        return "portable"
    here = Path(executable or sys.executable).parent

    def same(a, b):
        return os.path.normcase(os.path.abspath(str(a).rstrip("\\/"))) == os.path.normcase(os.path.abspath(str(b)))

    return "installed" if same(installed, here) else "portable"


def _installed_location():
    """Where the installer put PrivatePartyNet for this user (its InstallLocation), or "" when it has not."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "InstallLocation")
        return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) else ""
    except (ImportError, OSError):
        return ""


def sha256_file(path):
    with open(path, "rb") as file:
        return _sha256_of(file)


def _sha256_of(file):
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _held(path):
    """`path` open for reading, while no one else may write to, rename or delete it (others may still read it, which
    is how Windows starts it). Python's own open() lets others do all three."""
    if os.name != "nt":
        with open(path, "rb") as file:
            yield file
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE)
    generic_read, share_read, open_existing, normal = 0x80000000, 0x1, 3, 0x80
    handle = create(str(path), generic_read, share_read, None, open_existing, normal, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb") as file:
        yield file


# The folders come in environment variables (PPN_*), not written into the script: cmd reads a script in the console's
# code page and expands % signs in it, so a folder named with accents or a % would break the swap. Windows' own
# tasklist, find and ping are named in full: another find earlier on the PATH (Git's, say) would make it swap at once.
SWAP_SCRIPT = r"""@echo off
rem PrivatePartyNet's portable update: once the app has closed, swap its folder for the checked new one.
setlocal
set /a tries=0
:wait
"%SystemRoot%\System32\tasklist.exe" /FI "PID eq %PPN_PID%" 2>NUL | "%SystemRoot%\System32\find.exe" "%PPN_PID%" >NUL
if not errorlevel 1 (
  set /a tries+=1
  if %tries% GEQ 120 goto failed
  "%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >NUL
  goto wait
)
if exist "%PPN_OLD%" rmdir /s /q "%PPN_OLD%"
move "%PPN_APP%" "%PPN_OLD%" >NUL || goto failed
move "%PPN_NEW%" "%PPN_APP%" >NUL || (move "%PPN_OLD%" "%PPN_APP%" >NUL & goto failed)
if "%PPN_RESTART%"=="1" start "" "%PPN_APP%\%PPN_EXE%"
exit /b 0
:failed
if "%PPN_RESTART%"=="1" if exist "%PPN_APP%\%PPN_EXE%" start "" "%PPN_APP%\%PPN_EXE%"
exit /b 1
"""


# --- the updater ---------------------------------------------------------------------------------------------------


class Updater:
    """The app's updates. `settings()` gives the current settings dict, `save(**changes)` keeps changes, and `busy()`
    names what an install must wait for ("" when nothing). The other arguments are for tests: no packaged build can
    change the key, the address or the hosts, because only code passes them."""

    def __init__(self, settings, save, data_dir, busy, current=__version__, kind=None, api_url=RELEASES_URL,
                 api_hosts=API_HOSTS, asset_hosts=ASSET_HOSTS, fetcher=None, launcher=None,
                 app_dir=None, quit=None, clock=time.time):
        self._settings, self._save, self._busy = settings, save, busy
        self.current = str(current)
        self.kind = kind or install_kind()
        self.api_url, self.api_hosts, self.asset_hosts = api_url, frozenset(api_hosts), frozenset(asset_hosts)
        self.fetcher = fetcher or Fetcher()
        self._launch = launcher or self._spawn
        self._quit = quit or (lambda: None)
        self.app_dir = Path(app_dir) if app_dir else Path(sys.executable).parent
        self._clock = clock
        self.folder = Path(data_dir) / "updates"
        self._state_file = self.folder / "state.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._downloading = None  # the thread of a download started by a button, which returns before it ends
        self.found = None  # the release being offered: release_files' record, and the file to use
        self.ready = None  # Path of the checked download (installer) or unpacked folder (portable)
        self._unpacked = {}  # the unpacked folder's files (path inside it -> SHA-256), from the checked zip itself
        self.dismissed = None  # a version the person said Later to, with automatic updates off
        self.install_on_exit = False
        self.view_state = {"state": "idle", "message": "", "version": "", "notes": "", "checked_at": None}
        self._memory = self._load_state()

    # --- settings ---------------------------------------------------------------------------------------------

    @property
    def automatic(self):
        return self._settings().get("auto_update", True) is not False

    @property
    def include_test(self):
        return self._settings().get("update_prerelease", True) is not False

    def _load_state(self):
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # A damaged file keeps only what has the shape it should: the rest is as if never written.
        shapes = {"backoff_until": (int, float), "backoff": (int, float), "checked_at": (int, float), "etag": str,
                  "releases": list}
        state = {name: value for name, value in data.items()
                 if name in shapes and isinstance(value, shapes[name]) and not isinstance(value, bool)
                 and (not isinstance(value, float) or math.isfinite(value))}
        if "backoff_until" in state:  # never longer than the longest back-off from now
            state["backoff_until"] = min(state["backoff_until"], self._clock() + MAX_BACKOFF)
        return state

    def _store_state(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._memory), encoding="utf-8")
        except OSError:
            log.debug("Could not keep the update state", exc_info=True)

    # --- the schedule ------------------------------------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="privatepartynet-updates", daemon=True)
        self._thread.start()
        self.clean()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        delay = random.uniform(*FIRST_CHECK)
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self.automatic:
                try:
                    self.check(manual=False)
                except Exception:  # never let one bad check end the schedule
                    log.exception("Update check failed")
            delay = max(CHECK_SECONDS, self._memory.get("backoff_until", 0) - self._clock())

    # --- checking ----------------------------------------------------------------------------------------------

    def _set(self, state, message="", **more):
        with self._lock:
            self.view_state.update(state=state, message=message, **more)

    def check(self, manual=True):
        """Ask GitHub for releases, and take the newest genuine one further: download it at once when automatic
        updates are on, else offer it. Returns the view. A manual check is the "Check for updates" button."""
        with self._lock:  # one check at a time: the schedule, the control panel and the About page can all ask
            if self.view_state["state"] in ("checking", "downloading"):
                return self.view()
            before = dict(self.view_state)
            self._set("checking", "Checking for updates...")
        now = self._clock()
        until = self._memory.get("backoff_until", 0)
        if now < until:  # GitHub asked to wait: neither the schedule nor the button asks again before then
            with self._lock:
                self.view_state = before
            if manual:
                self._set("error", f"GitHub asked us to wait; try again after {_clock_time(until, now)}.")
            return self.view()
        try:
            found = self._find()
        except UpdateError as error:
            log.info("Update check: %s", error)
            self._set("error", f"Could not check for updates: {error}", checked_at=now)
            return self.view()
        except Exception:  # anything unforeseen must not leave the view stuck at "checking"
            log.exception("Update check failed")
            self._set("error", "Could not check for updates: something unexpected went wrong (see the log).",
                      checked_at=now)
            return self.view()
        self.view_state["checked_at"] = now
        self._memory["checked_at"] = now
        self._store_state()
        if found is None:
            self._set("current", f"{APP} {self.current} is the newest version.")
            return self.view()
        with self._lock:
            known = self.found and self.found["version"] == found["version"]
            self.found = found
        if known and self.ready is not None:
            self._offer_ready()  # the same version, already checked and waiting
            return self.view()
        if self.automatic:
            self.download(background=manual)  # a button's call returns now; the panel shows the download's progress
        else:
            self._set("available", f"{APP} {found['version']} is out.", version=str(found["version"]),
                      notes=found["notes"])
        return self.view()

    def _find(self):
        headers = {"Accept": "application/vnd.github+json"}
        if self._memory.get("etag") and self._memory.get("releases") is not None:
            headers["If-None-Match"] = self._memory["etag"]
        status, answer, body = self.fetcher.get(self.api_url, self.api_hosts, headers, limit=MAX_LISTING)
        if status in (403, 429):
            self._back_off(answer)
            raise UpdateError("GitHub asked to wait before checking again")
        if status == 304:
            releases = self._memory.get("releases") or []
        elif status == 200:
            try:
                releases = json.loads(body)
            except ValueError:
                raise UpdateError("GitHub's answer could not be read") from None
            if not isinstance(releases, list):
                raise UpdateError("GitHub's answer is not a list of releases")
            self._memory["etag"] = answer.get("ETag") or ""
            self._memory["releases"] = [_slim(r) for r in releases if isinstance(r, dict)][:30]
            self._memory.pop("backoff_until", None)
            self._memory.pop("backoff", None)
        else:
            raise UpdateError(f"GitHub answered {status}")
        why = None
        for release in candidates(releases, self.current, self.include_test, self._settings().get("update_skip") or ""):
            try:
                return self._check_release(release)
            except UpdateError as error:
                log.warning("Release %s refused: %s", release.get("tag_name"), error)
                why = error
        if why is not None:
            raise why
        return None

    def _back_off(self, headers):
        now = self._clock()
        wait = min(MAX_BACKOFF, max(CHECK_SECONDS, 2 * float(self._memory.get("backoff") or CHECK_SECONDS / 2)))
        try:
            wait = max(wait, float(headers.get("Retry-After") or 0))
        except ValueError:
            try:
                wait = max(wait, parsedate_to_datetime(headers["Retry-After"]).timestamp() - now)
            except (TypeError, ValueError, KeyError):
                pass
        try:
            wait = max(wait, float(headers.get("X-RateLimit-Reset") or 0) - now)
        except ValueError:
            pass
        wait = min(wait, MAX_BACKOFF)
        self._memory.update(backoff=wait, backoff_until=now + wait)
        self._store_state()

    def _check_release(self, release):
        found = release_files(release, self.current)
        kind = "portable" if self.kind == "portable" else "installer"
        if kind not in found["files"]:
            raise UpdateError(f"the release has no {kind}")
        entry = found["files"][kind]
        found.update(kind=kind, file=entry, url=asset(release, entry["name"]).get("browser_download_url"))
        return found

    # --- downloading -------------------------------------------------------------------------------------------

    def download(self, background=False):
        """Fetch the offered file, check its size and hash, and (portable) unpack it. Then offer Restart now. With
        `background`, the state says "downloading" at once and the rest runs in its own thread (join_download)."""
        found = self.found
        if found is None:
            return self.view()
        if self.kind == "source":
            self._set("available", f"{APP} {found['version']} is out. This copy runs from source: update it with git.",
                      version=str(found["version"]), notes=found["notes"])
            return self.view()
        version, entry = found["version"], found["file"]
        with self._lock:  # Update pressed while a download is already under way: that one carries on
            if self.view_state["state"] == "downloading":
                return self.view()
            self._set("downloading", f"Downloading {APP} {version}...", version=str(version), notes=found["notes"],
                      progress=0.0)
        if background:
            self._downloading = threading.Thread(target=self._fetch, args=(found,), name="privatepartynet-update-download",
                                                 daemon=True)
            self._downloading.start()
            return self.view()
        return self._fetch(found)

    def join_download(self, timeout=None):
        """Wait for a download started in the background to end."""
        thread = self._downloading
        if thread is not None:
            thread.join(timeout)

    def _progress(self, version, size):
        """A callback for the fetcher: the share downloaded, in the view's progress (0 to 1) and message."""
        shown = [-1]

        def step(done):
            percent = min(100, done * 100 // max(1, size))
            if percent != shown[0]:
                shown[0] = percent
                self._set("downloading", f"Downloading {APP} {version}... {percent}%", progress=percent / 100)
        return step

    def _fetch(self, found):
        version, entry = found["version"], found["file"]
        target = self.folder / entry["name"]
        partial = target.with_name(target.name + ".part")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if not (target.is_file() and target.stat().st_size == entry["size"] and sha256_file(target) == entry["sha256"]):
                with open(partial, "wb") as file:
                    status, _, size = self.fetcher.get(found["url"], self.asset_hosts, limit=entry["size"], into=file,
                                                       progress=self._progress(version, entry["size"]))
                if status != 200 or size != entry["size"]:
                    raise UpdateError(f"the download stopped short ({status}, {size} of {entry['size']} bytes)")
                if sha256_file(partial) != entry["sha256"]:
                    raise UpdateError("the download is not the file GitHub published (its SHA-256 differs)")
                os.replace(partial, target)
            ready = self._unpack(target, version) if found["kind"] == "portable" else target
        except (UpdateError, OSError, zipfile.BadZipFile) as error:
            partial.unlink(missing_ok=True)
            log.warning("Update download: %s", error)
            self._set("error", f"The update could not be downloaded: {error}")
            return self.view()
        with self._lock:
            self.ready = ready
        self.clean()
        self._offer_ready()
        return self.view()

    def _offer_ready(self):
        version = self.found["version"]
        waiting = self._busy()
        if self.found["kind"] == "portable" and self._in_updates() is not None:
            self._set("manual", f"{APP} {version} is checked and unpacked, but this copy's folder cannot be changed "
                      f"in place. Close {APP} and replace the folder with the one that opens.", version=str(version),
                      waiting="")
            return
        self._set("ready", ready_message(version, waiting), version=str(version), waiting=waiting)

    def _unpack(self, archive, version):
        """Unpack a checked portable zip beside this copy's folder (or, when that cannot be written, into the updates
        folder). Returns the unpacked app folder."""
        parent = self.app_dir.parent
        staging = parent / f"{self.app_dir.name}.update-{version}"
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir()
        except OSError:
            staging = self.folder / f"{APP}-{version}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
        (staging / STAGING_MARK).write_text(f"Unpacked by {APP}'s updater: {version}\n", encoding="utf-8")
        root = staging.resolve()
        with zipfile.ZipFile(archive) as zipped:
            total = 0
            for info in zipped.infolist():
                destination = (staging / info.filename).resolve()
                if root not in destination.parents and destination != root:
                    raise UpdateError("the zip names a file outside its folder")
                total += info.file_size
                if total > MAX_FILE * 2:
                    raise UpdateError("the zip unpacks to more than it may")
            zipped.extractall(staging)
            unpacked = {}
            for info in zipped.infolist():
                parts = Path(info.filename).parts
                if info.is_dir() or len(parts) < 2 or parts[0] != APP:
                    continue
                digest = hashlib.sha256()
                with zipped.open(info) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b""):
                        digest.update(chunk)
                unpacked[Path(*parts[1:]).as_posix().lower()] = digest.hexdigest()
        app = staging / APP
        if not (app / f"{APP}.exe").is_file():
            raise UpdateError("the zip does not hold PrivatePartyNet")
        self._unpacked = unpacked
        return app

    def _check_unpacked(self, folder):
        """Refuse an unpacked folder whose files are not exactly the checked zip's: one changed, added or removed since
        it was unpacked. It may have waited days beside the app's folder for the app to close."""
        found = {}
        for path in folder.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise UpdateError("the unpacked update changed on disk (a link appeared in it)")
            if path.is_file():
                found[path.relative_to(folder).as_posix().lower()] = path
        if set(found) != set(self._unpacked) or any(sha256_file(found[name]) != digest
                                                    for name, digest in self._unpacked.items()):
            raise UpdateError("the unpacked update changed on disk since it was checked")

    # --- installing ----------------------------------------------------------------------------------------------

    def restart_now(self):
        """The person pressed Restart now (or Update, with automatic updates off): install and start again, unless
        something must finish first."""
        if self.ready is None:
            if self.found is not None and not self.automatic:
                self.download()
                if self.ready is None:
                    return {"ok": False, "message": self.view_state["message"]}
            else:
                return {"ok": False, "message": "No update is ready."}
        waiting = self._busy()
        if waiting:
            self.install_on_exit = True
            self._offer_ready()
            return {"ok": False, "message": ready_message(self.found["version"], waiting)}
        try:
            self._install(restart=True)
        except UpdateError as error:
            return {"ok": False, "message": f"The update was not installed: {error}."}
        return {"ok": True, "message": f"Installing {APP} {self.found['version']}..."}

    def later(self):
        if self.ready is not None:
            self.install_on_exit = True
            self._set("ready", f"{APP} {self.found['version']} installs when {APP} closes.",
                      version=str(self.found["version"]), waiting=self._busy())
        elif self.found is not None:
            self.dismissed = str(self.found["version"])
            self._set("dismissed", "", version=str(self.found["version"]))
        return self.view()

    def skip(self):
        if self.found is not None:
            self._save(update_skip=str(self.found["version"]))
            self.found, self.ready, self.install_on_exit = None, None, False
            self._set("skipped", "This version is skipped; a newer one is offered when it comes.")
        return self.view()

    def busy_changed(self):
        """Call when a party, a call or the game network ends: a waiting install may go ahead now."""
        if self.ready is not None and self.view_state["state"] == "ready":
            self._offer_ready()

    def on_exit(self):
        """The app is closing: install what is checked and waiting, without starting the app again."""
        if self.ready is not None and (self.install_on_exit or self.automatic) and self.view_state["state"] == "ready":
            try:
                self._install(restart=False, quitting=True)
            except UpdateError as error:
                log.warning("The update could not be installed on exit: %s", error)

    def _install(self, restart, quitting=False):
        try:
            self._start_install(restart)
        except OSError as error:  # the file is gone, the disk is full, or Windows would not start it
            self.ready = None
            self._set("error", f"The update could not be started: {error}")
            raise UpdateError(f"it could not be started ({error.strerror or error})") from None
        log.info("Installing %s %s (%s)", APP, self.found["version"], self.found["kind"])
        self._set("installing", f"Installing {APP} {self.found['version']}...")
        if not quitting:
            self._quit()

    def _start_install(self, restart):
        found, ready = self.found, self.ready
        if found["kind"] == "installer":
            # Hashed again just before it runs, through a handle that keeps anyone from changing or replacing it until
            # Windows has started it.
            with _held(ready) as file:
                if _sha256_of(file) != found["file"]["sha256"]:
                    self.ready = None
                    self._set("error", "The downloaded installer changed on disk; it was not run.")
                    raise UpdateError("the installer's SHA-256 changed since it was checked")
                arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"] + \
                    ([] if restart else ["/NOLAUNCH=1"])
                self._launch([str(ready), *arguments], None)
        else:
            try:  # checked again just before it is swapped in
                self._check_unpacked(ready)
            except UpdateError:
                self.ready = None
                self._set("error", "The unpacked update changed on disk; it was not installed.")
                raise
            script = self.folder / "swap.cmd"
            old = self.app_dir.parent / f"{self.app_dir.name}.old"
            script.write_text(SWAP_SCRIPT, encoding="ascii")
            self._launch(["cmd.exe", "/c", str(script)],
                         {"PPN_PID": str(os.getpid()), "PPN_OLD": str(old), "PPN_APP": str(self.app_dir),
                          "PPN_NEW": str(ready), "PPN_RESTART": "1" if restart else "0", "PPN_EXE": f"{APP}.exe"})

    def _spawn(self, command, env):
        """Start `command` on its own; `env` adds variables for it (the portable swap's folders), never read back."""
        subprocess.Popen(command, close_fds=True, creationflags=DETACHED | NO_WINDOW, cwd=str(self.folder),
                         env={**os.environ, **env} if env else None)

    def _in_updates(self):
        """The name of the updates folder's entry that holds what is ready, when it is in there (an installer, or a
        portable copy unpacked there because its own folder's parent cannot be written); else None."""
        ready = self.ready
        if ready is None:
            return None
        try:
            return ready.relative_to(self.folder).parts[0]
        except (ValueError, IndexError):
            return None

    def clean(self):
        """Delete old downloads, a portable copy's previous folder once the new one runs, and versions unpacked beside
        it that are no longer wanted (skipped, or passed by a newer one)."""
        keep = {self._in_updates(), self.found["file"]["name"] if self.found else None, "state.json"}
        try:
            for path in self.folder.iterdir():
                if path.name in keep:
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        except OSError:
            pass
        old = self.app_dir.parent / f"{self.app_dir.name}.old"
        if self.kind == "portable" and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        if self.kind == "portable":
            self._clean_staging()

    def _clean_staging(self):
        """Remove <app>.update-<version> folders beside this copy, but only those named exactly so, with a real version,
        that hold the updater's mark, and are not links; and never the one that is ready now."""
        current = self.ready.parent if self.ready is not None else None
        prefix = f"{self.app_dir.name}.update-"
        try:
            entries = list(self.app_dir.parent.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.name.startswith(prefix) or parse_version(path.name[len(prefix):]) is None \
                    or path.name[len(prefix):] != path.name[len(prefix):].strip() or path == current:
                continue
            try:
                if path.is_symlink() or path.is_junction() or not path.is_dir():
                    continue
                mark = path / STAGING_MARK
                if mark.is_symlink() or not mark.is_file():
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)

    # --- for the windows ---------------------------------------------------------------------------------------

    def view(self):
        with self._lock:
            view = dict(self.view_state)
        view.update(automatic=self.automatic, include_test=self.include_test, current=self.current, kind=self.kind,
                    checked_at=view.get("checked_at") or self._memory.get("checked_at"))
        if view["state"] == "available" and self.dismissed == view.get("version"):
            view["state"] = "dismissed"
        return view


def _clock_time(when, now):
    """`when` (seconds since 1970) as this PC's HH:MM, with " tomorrow" when that is not today."""
    moment, today = time.localtime(when), time.localtime(now)
    later = (moment.tm_year, moment.tm_yday) != (today.tm_year, today.tm_yday)
    return time.strftime("%H:%M", moment) + (" tomorrow" if later else "")


def _slim(release):
    """What is kept of a release between checks (for answers of 304, Not Modified)."""
    return {"tag_name": release.get("tag_name"), "draft": bool(release.get("draft")),
            "prerelease": bool(release.get("prerelease")), "body": plain_notes(release.get("body")),
            "assets": [{"name": a.get("name"), "size": a.get("size"), "digest": a.get("digest"),
                        "browser_download_url": a.get("browser_download_url")}
                       for a in _list(release.get("assets")) if isinstance(a, dict)]}
