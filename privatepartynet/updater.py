"""Updates: finding a newer PrivatePartyNet on GitHub, checking that it really is one, and installing it.

What it does, when automatic updates are on (the default): shortly after the app starts, and every six hours, it asks
GitHub's public API for the releases of PrivatePartyNet/PrivatePartyNet (test versions too, unless that is turned
off). A newer one is downloaded in the background and checked, and then the control panel offers it: Restart now, or
Later, which installs it when the app is next closed. With automatic updates off, nothing is asked of GitHub until
Check for updates is pressed; a newer version then offers Update, Later or Skip this version.

Why a download can be trusted: every release carries PrivatePartyNet-<version>-update.json, which names each file
with its size and SHA-256, and a detached Ed25519 signature of it (.json.sig). The public key is part of the app
(TRUSTED_KEYS) and nothing at run time can replace it: no setting, no environment variable, no command line. A
download is refused unless the signature checks out with that key, its size and hash are the ones signed, the version
is newer than the one running (never older: no rollback) and the manifest's version and tag are the release's. The
file is hashed again just before it runs. Only HTTPS is used, with Windows' certificates, and files only come from
GitHub's own hosts. A signed manifest may carry the next public key, signed by the current one, which is then trusted
too (key rotation; kept in the data folder).

Installing: an installed copy runs the checked installer for this user, silently with its progress showing, and the
installer starts the app again. A portable copy unpacks the checked zip beside its own folder and swaps the two once
the app has closed (the user's data is in %LOCALAPPDATA%, not in the app's folder, so it stays as it is). Nothing is
installed during a party, a call or while the game network is on; the control panel says it is waiting.
"""

import hashlib
import json
import logging
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
from privatepartynet.identity import verify

log = logging.getLogger("privatepartynet.updater")

APP = "PrivatePartyNet"
REPOSITORY = "PrivatePartyNet/PrivatePartyNet"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
API_HOSTS = frozenset({"api.github.com"})
ASSET_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
# The release signing key's public half (tools/sign_release.py --public). Only these, and keys they hand on, are trusted.
TRUSTED_KEYS = ("88d103cbd9024db9a709a330d229fc7542e482efda81cc3983f4aecbc88cda7c",)
MANIFEST_LABEL = b"PrivatePartyNet update manifest|"
NEXT_KEY_LABEL = b"PrivatePartyNet update key|"
FIRST_CHECK = (60.0, 180.0)  # seconds after the app starts, picked at random, so not every copy asks at once
CHECK_SECONDS = 6 * 3600
MAX_BACKOFF = 24 * 3600
MAX_LISTING = 2 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_FILE = 2 * 1024 ** 3
MAX_NOTES = 4000
KINDS = ("installer", "portable")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class UpdateError(Exception):
    """Why an update was refused or could not be fetched, in plain words."""


# --- versions -----------------------------------------------------------------------------------------------------


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


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


def manifest_name(version):
    return f"{APP}-{version}-update.json"


def file_names(version):
    return {"installer": f"{APP}Setup-{version}.exe", "portable": f"{APP}-{version}-portable.zip"}


# --- checking a release ----------------------------------------------------------------------------------------------


def check_manifest(data, signature, keys, release, current):
    """The manifest of `release` (a GitHub release record), when it is genuine and newer than `current`; else
    UpdateError. `data` is the manifest file as downloaded, `signature` its .sig file."""
    text = signature.decode("ascii", "replace").strip() if isinstance(signature, bytes) else str(signature).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{128}", text):
        raise UpdateError("the update's signature is malformed")
    if not any(verify(key, text, MANIFEST_LABEL + data) for key in keys):
        raise UpdateError("the update's signature does not check out; it was not made by PrivatePartyNet's key")
    try:
        manifest = json.loads(data)
    except ValueError:
        raise UpdateError("the update's manifest cannot be read") from None
    if not isinstance(manifest, dict) or manifest.get("app") != APP:
        raise UpdateError("the manifest is not PrivatePartyNet's")
    version = parse_version(manifest.get("version"))
    tag = str(release.get("tag_name") or "")
    if version is None or manifest.get("tag") != tag or parse_version(tag) != version or manifest["version"] != str(version):
        raise UpdateError("the manifest's version and tag are not the release's")
    if not version > Version(current):
        raise UpdateError(f"{version} is not newer than {current}; an update never goes back")
    files = {}
    for entry in manifest.get("files") or []:
        try:
            name, size, digest, kind = entry["name"], int(entry["size"]), str(entry["sha256"]).lower(), entry["kind"]
        except (KeyError, TypeError, ValueError):
            raise UpdateError("the manifest's file list is damaged") from None
        if kind not in KINDS or name != file_names(version)[kind] or not 0 < size <= MAX_FILE \
                or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError(f"the manifest's entry for {name!r} is not one this app accepts")
        files[kind] = {"name": name, "size": size, "sha256": digest}
    if not files:
        raise UpdateError("the manifest lists no files")
    next_key = manifest.get("next_key")
    learned = None
    if next_key is not None:
        try:
            key, sig = str(next_key["key"]), str(next_key["sig"])
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not any(verify(k, sig, NEXT_KEY_LABEL + bytes.fromhex(key))
                                                                   for k in keys):
                raise ValueError("next key")
        except (KeyError, TypeError, ValueError):
            raise UpdateError("the manifest's next key is not signed by the current one") from None
        learned = key
    notes = manifest.get("notes") if isinstance(manifest.get("notes"), str) else str(release.get("body") or "")
    return {"version": version, "tag": tag, "files": files, "next_key": learned, "notes": plain_notes(notes),
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

    def get(self, url, hosts, headers=None, limit=MAX_MANIFEST, into=None):
        """(status, headers, body). With `into` (a file object), the body is written there instead and body is the
        number of bytes. Raises UpdateError on a host that is not allowed or a body over `limit`."""
        if not _allowed(url, hosts):
            raise UpdateError(f"{urllib.parse.urlsplit(str(url)).hostname} is not a host updates come from")
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.context), _Redirects(hosts))
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{__version__}", **(headers or {})})
        try:
            response = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers or {}), b""
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
                else:
                    chunks.append(chunk)
            return response.status, dict(response.headers), total if into is not None else b"".join(chunks)


# --- where this copy runs ------------------------------------------------------------------------------------------


def install_kind():
    """"installed" (by the installer), "portable" (unzipped anywhere) or "source" (a checkout, which git updates)."""
    if not getattr(sys, "frozen", False):
        return "source"
    return "installed" if (Path(sys.executable).parent / "unins000.exe").exists() else "portable"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
                 keys=TRUSTED_KEYS, api_hosts=API_HOSTS, asset_hosts=ASSET_HOSTS, fetcher=None, launcher=None,
                 app_dir=None, quit=None, clock=time.time):
        self._settings, self._save, self._busy = settings, save, busy
        self.current = str(current)
        self.kind = kind or install_kind()
        self.api_url, self.api_hosts, self.asset_hosts = api_url, frozenset(api_hosts), frozenset(asset_hosts)
        self.compiled_keys = tuple(keys)
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
        self.found = None  # the checked release being offered: check_manifest's record, and the file to use
        self.ready = None  # Path of the checked download (installer) or unpacked folder (portable)
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

    def keys(self):
        """The compiled-in keys and those they handed on (key rotation)."""
        learned = [k for k in self._memory.get("keys", []) if isinstance(k, str) and re.fullmatch(r"[0-9a-f]{64}", k)]
        return tuple(dict.fromkeys(self.compiled_keys + tuple(learned)))

    def _load_state(self):
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

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
        if not manual and now < self._memory.get("backoff_until", 0):
            with self._lock:
                self.view_state = before
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
            self.download()
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
            self._memory["etag"] = answer.get("ETag") or answer.get("etag") or ""
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
            wait = max(wait, float(headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset") or 0) - now)
        except ValueError:
            pass
        wait = min(wait, MAX_BACKOFF)
        self._memory.update(backoff=wait, backoff_until=now + wait)
        self._store_state()

    def _check_release(self, release):
        version = parse_version(release.get("tag_name"))
        name = manifest_name(version)
        manifest, signature = asset(release, name), asset(release, name + ".sig")
        if manifest is None or signature is None:
            raise UpdateError(f"the release has no signed manifest ({name})")
        data = self._small(manifest)
        sig = self._small(signature)
        found = check_manifest(data, sig, self.keys(), release, self.current)
        if found["next_key"] and found["next_key"] not in self.keys():
            self._memory.setdefault("keys", []).append(found["next_key"])
            self._store_state()
            log.info("Update key rotation: a new signing key is now trusted")
        kind = "portable" if self.kind == "portable" else "installer"
        if kind not in found["files"]:
            raise UpdateError(f"the release has no {kind}")
        entry = found["files"][kind]
        download = asset(release, entry["name"])
        if download is None or _size(download.get("size")) != entry["size"]:
            raise UpdateError(f"the release's {entry['name']} is not the one the manifest signs")
        found.update(kind=kind, file=entry, url=download.get("browser_download_url"))
        return found

    def _small(self, item):
        status, _, body = self.fetcher.get(item.get("browser_download_url"), self.asset_hosts, limit=MAX_MANIFEST)
        if status != 200:
            raise UpdateError(f"{item.get('name')} could not be downloaded ({status})")
        return body

    # --- downloading -------------------------------------------------------------------------------------------

    def download(self):
        """Fetch the offered file, check its size and hash, and (portable) unpack it. Then offer Restart now."""
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
            self._set("downloading", f"Downloading {APP} {version}...", version=str(version), notes=found["notes"])
        target = self.folder / entry["name"]
        partial = target.with_name(target.name + ".part")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if not (target.is_file() and target.stat().st_size == entry["size"] and sha256_file(target) == entry["sha256"]):
                with open(partial, "wb") as file:
                    status, _, size = self.fetcher.get(found["url"], self.asset_hosts, limit=entry["size"], into=file)
                if status != 200 or size != entry["size"]:
                    raise UpdateError(f"the download stopped short ({status}, {size} of {entry['size']} bytes)")
                if sha256_file(partial) != entry["sha256"]:
                    raise UpdateError("the download is not the file the release signs (its SHA-256 differs)")
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
        message = f"{APP} {version} is ready." + (f" It installs when {waiting} ends." if waiting else "")
        self._set("ready", message, version=str(version), waiting=waiting)

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
        app = staging / APP
        if not (app / f"{APP}.exe").is_file():
            raise UpdateError("the zip does not hold PrivatePartyNet")
        return app

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
            return {"ok": False, "message": f"The update waits until {waiting} ends; it installs then or when "
                                            f"{APP} closes."}
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
            if sha256_file(ready) != found["file"]["sha256"]:  # hashed again just before it runs
                self.ready = None
                self._set("error", "The downloaded installer changed on disk; it was not run.")
                raise UpdateError("the installer's SHA-256 changed since it was checked")
            arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"] + ([] if restart else ["/NOLAUNCH=1"])
            self._launch([str(ready), *arguments], None)
        else:
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
        """Delete old downloads, and a portable copy's previous folder once the new one runs."""
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

    # --- for the windows ---------------------------------------------------------------------------------------

    def view(self):
        with self._lock:
            view = dict(self.view_state)
        view.update(automatic=self.automatic, include_test=self.include_test, current=self.current, kind=self.kind,
                    checked_at=view.get("checked_at") or self._memory.get("checked_at"))
        if view["state"] == "available" and self.dismissed == view.get("version"):
            view["state"] = "dismissed"
        return view


def _slim(release):
    """What is kept of a release between checks (for answers of 304, Not Modified)."""
    return {"tag_name": release.get("tag_name"), "draft": bool(release.get("draft")),
            "prerelease": bool(release.get("prerelease")), "body": plain_notes(release.get("body")),
            "assets": [{"name": a.get("name"), "size": a.get("size"), "browser_download_url": a.get("browser_download_url")}
                       for a in _list(release.get("assets")) if isinstance(a, dict)]}
