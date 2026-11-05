"""Updates: version order, what a release offers (only with the size and SHA-256 GitHub publishes for each file),
waiting for a party, and the whole flow against a fake GitHub on this PC (HTTPS, its address passed in by code, as only
tests can)."""

import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from privatepartynet import updater
from privatepartynet.cert import ensure_certificate
from privatepartynet.updater import Fetcher, UpdateError, Updater, Version, candidates, release_files


def entry(version, kind, content):
    name = updater.file_names(version)[kind]
    return {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "kind": kind}


def release(version, prerelease=False, draft=False, assets=(), body="notes"):
    """A GitHub release record; each asset is (name, size, url, digest), the digest as GitHub gives it ("sha256:...")."""
    return {"tag_name": "v" + version, "prerelease": prerelease, "draft": draft, "body": body,
            "assets": [{"name": n, "size": s, "browser_download_url": u, "digest": d} for n, s, u, d in assets]}


class VersionTests(unittest.TestCase):
    def test_versions_sort_as_semantic_versioning_says_tags_included(self):
        order = ["3.1.0", "v3.2.0-alpha.1", "3.2.0-alpha.2", "3.2.0-alpha.10", "3.2.0-beta", "3.2.0-rc.1", "v3.2.0",
                 "3.2.1", "3.10.0"]
        versions = [Version(v) for v in order]
        self.assertEqual(versions, sorted(reversed(versions)))
        self.assertEqual(Version("v3.2.0"), Version("3.2.0"))
        self.assertEqual(Version("3.2.0+build.7"), Version("3.2.0"))
        self.assertTrue(Version("3.2.0-alpha.1").prerelease)
        for bad in ("3.2", "version 3", "3.2.0.1", "", "v٣.2.0", "3.２.0", "vv3.2.0"):  # digits, not ASCII
            self.assertIsNone(updater.parse_version(bad))


class ReleaseTests(unittest.TestCase):
    CONTENT = b"the installer"

    def offer(self, version="3.2.1", current="3.2.0", assets=None, body="Fixes."):
        if assets is None:
            assets = [(updater.file_names(version)["installer"], len(self.CONTENT), "https://github.com/x",
                       "sha256:" + hashlib.sha256(self.CONTENT).hexdigest())]
        return release_files(release(version, assets=assets, body=body), current)

    def test_a_release_offers_its_files_with_the_size_and_hash_github_publishes(self):
        found = self.offer()
        self.assertEqual(str(found["version"]), "3.2.1")
        self.assertEqual(found["files"], {"installer": {"name": "PrivatePartyNetSetup-3.2.1.exe", "size": len(self.CONTENT),
                                                        "sha256": hashlib.sha256(self.CONTENT).hexdigest()}})
        self.assertEqual(found["notes"], "Fixes.")

    def test_no_rollback_and_no_same_version(self):
        for current in ("3.2.1", "3.3.0"):
            with self.subTest(current=current):
                with self.assertRaisesRegex(UpdateError, "not newer"):
                    self.offer(current=current)

    def test_a_file_without_githubs_size_or_hash_is_refused(self):
        name = "PrivatePartyNetSetup-3.2.1.exe"
        good = "sha256:" + "a" * 64
        for size, digest in ((13, None), (13, "md5:" + "a" * 32), (13, "sha256:xyz"), (0, good), (True, good), ("13", good)):
            with self.subTest(size=size, digest=digest):
                with self.assertRaisesRegex(UpdateError, "no size or SHA-256"):
                    self.offer(assets=[(name, size, "https://github.com/x", digest)])

    def test_only_the_names_the_build_gives_count(self):
        good = "sha256:" + "a" * 64
        with self.assertRaisesRegex(UpdateError, "no installer and no portable"):
            self.offer(assets=[("evil.exe", 13, "https://github.com/x", good),
                               ("PrivatePartyNetSetup-3.2.2.exe", 13, "https://github.com/y", good)])

    def test_release_notes_are_plain_text_and_short(self):
        found = self.offer(body="<b>bold</b>\x1b[31m" + "x" * 5000)
        self.assertTrue(found["notes"].startswith("<b>bold</b>[31m"), "kept as text, control characters dropped")
        self.assertLessEqual(len(found["notes"]), updater.MAX_NOTES + 3)


class CandidateTests(unittest.TestCase):
    RELEASES = [release("3.2.2", draft=True), release("3.3.0-alpha.1", prerelease=True), release("3.2.1"),
                release("3.1.9"), {"tag_name": "nonsense"}]

    def tags(self, **options):
        return [r["tag_name"] for r in candidates(self.RELEASES, "3.2.0", **options)]

    def test_drafts_are_skipped_and_older_versions_too(self):
        self.assertEqual(self.tags(include_test=True), ["v3.3.0-alpha.1", "v3.2.1"])

    def test_test_versions_only_when_the_switch_is_on(self):
        self.assertEqual(self.tags(include_test=False), ["v3.2.1"])

    def test_a_skipped_version_is_not_offered(self):
        self.assertEqual(self.tags(include_test=False, skipped="3.2.1"), [])


class InstallKindTests(unittest.TestCase):
    """Installed or portable comes from the installer's own record in the registry (HKCU, its AppId) and where it
    installed, not from an uninstaller file that any folder could hold."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Path(temp.name) / "Programs" / "PrivatePartyNet"
        self.app.mkdir(parents=True)
        self.exe = str(self.app / "PrivatePartyNet.exe")

    def kind(self, location, frozen=True):
        return updater.install_kind(executable=self.exe, frozen=frozen, location=lambda: location)

    def test_the_registry_record_of_this_folder_makes_it_installed(self):
        self.assertEqual(self.kind(str(self.app) + "\\"), "installed")
        self.assertEqual(self.kind(str(self.app).upper()), "installed", "Windows paths are not case-sensitive")

    def test_an_uninstaller_file_alone_does_not(self):
        (self.app / "unins000.exe").write_bytes(b"")
        self.assertEqual(self.kind(""), "portable")
        self.assertEqual(self.kind(str(self.app.parent / "Elsewhere")), "portable", "another folder is installed")

    def test_a_checkout_is_source(self):
        self.assertEqual(self.kind(str(self.app), frozen=False), "source")

    def test_the_registry_key_is_the_installers_own(self):
        iss = (Path(updater.__file__).parents[1] / "installer" / "PrivatePartyNet.iss").read_text(encoding="utf-8")
        app_id = iss.split("AppId={{")[1].split("}")[0]
        self.assertEqual(updater.UNINSTALL_KEY, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{{{app_id}}}_is1")


class NoBackDoorTests(unittest.TestCase):
    def test_no_environment_variable_or_setting_changes_the_address(self):
        source = Path(updater.__file__).read_text(encoding="utf-8")
        # The one use of os.environ hands it on, with the swap's folders added, to the process that swaps them.
        spawn = "env={**os.environ, **env} if env else None"
        self.assertEqual(source.count(spawn), 1)
        for reading in ("os.environ", "getenv", "sys.argv"):
            self.assertNotIn(reading, source.replace(spawn, ""))
        app = (Path(updater.__file__).parent / "app.py").read_text(encoding="utf-8")
        call = app[app.index("self.updater = Updater("):app.index("self.updater = Updater(") + 220]
        for argument in ("api_url", "keys", "hosts", "fetcher"):
            self.assertNotIn(argument, call, "the app passes only settings, folders and callbacks")
        self.assertEqual(updater.RELEASES_URL, "https://api.github.com/repos/PrivatePartyNet/PrivatePartyNet/releases?per_page=30")
        self.assertEqual(updater.API_HOSTS, {"api.github.com"})

    def test_only_https_on_allowed_hosts(self):
        fetcher = Fetcher()
        for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x"):
            with self.assertRaisesRegex(UpdateError, "not a host"):
                fetcher.get(url, updater.ASSET_HOSTS)


# --- a fake GitHub on this PC --------------------------------------------------------------------------------------


class FakeGitHub:
    """Releases at /releases (with ETag and 304), and their assets under /assets/. `status` forces an answer."""

    def __init__(self, folder):
        self.releases, self.files, self.status, self.calls, self.redirect = [], {}, None, [], {}
        self.wait_header = ("Retry-After", "3600")
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.calls.append((self.path, self.headers.get("If-None-Match")))
                if server.status:
                    self.send_response(server.status)
                    self.send_header(*server.wait_header)
                    self.end_headers()
                    return
                if self.path.startswith("/releases"):
                    body = json.dumps(server.releases).encode()
                    etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("ETag", etag)
                elif self.path in server.redirect:
                    self.send_response(302)
                    self.send_header("Location", server.redirect[self.path])
                    self.end_headers()
                    return
                elif self.path in server.files:
                    body = server.files[self.path]
                    self.send_response(200)
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.certfile, self.keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certfile, self.keyfile)
        self.http.socket = context.wrap_socket(self.http.socket, server_side=True)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"https://127.0.0.1:{self.http.server_address[1]}"

    def client_context(self):
        context = ssl.create_default_context(cafile=str(self.certfile))
        context.check_hostname = False  # the test certificate names localhost; it is still the one pinned as the CA
        return context

    def publish(self, version, payloads, prerelease=False, digests=True):
        """A release of `version` whose files are `payloads` {kind: bytes}, each with the SHA-256 GitHub gives
        (digests=False: a release whose files have none)."""
        files = [entry(version, kind, content) for kind, content in payloads.items()]
        assets = []
        for item in files:
            self.files[f"/assets/{item['name']}"] = payloads[item["kind"]]
            assets.append((item["name"], item["size"], f"{self.base}/assets/{item['name']}",
                           "sha256:" + item["sha256"] if digests else None))
        self.releases.insert(0, release(version, prerelease=prerelease, assets=assets))

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class FlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.github = FakeGitHub(self.folder)
        self.addCleanup(self.github.close)
        self.settings = {"auto_update": True, "update_prerelease": True, "update_skip": ""}
        self.busy = ""
        self.launched, self.quits, self.envs = [], [], []

    def updater(self, kind="installed", app_dir=None):
        up = Updater(lambda: self.settings, lambda **c: self.settings.update(c), self.folder / "data",
                     lambda: self.busy, current="3.2.0", kind=kind, api_url=f"{self.github.base}/releases",
                     api_hosts={"127.0.0.1"}, asset_hosts={"127.0.0.1"},
                     fetcher=Fetcher(self.github.client_context(), timeout=5), launcher=self.launch,
                     app_dir=app_dir or self.folder / "app" / "PrivatePartyNet", quit=lambda: self.quits.append(1))
        self.addCleanup(up.join_download, 30)  # a button's check downloads in the background: let it end first
        return up

    def launch(self, command, env=None):
        self.launched.append(command)
        self.envs.append(env)

    def portable(self, app, files=None):
        """A portable copy at `app` and a release of 3.2.1 whose zip holds `files` {name: bytes}."""
        app.mkdir(parents=True)
        (app / "PrivatePartyNet.exe").write_bytes(b"old app")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in (files or {"PrivatePartyNet/PrivatePartyNet.exe": b"new app",
                                            "PrivatePartyNet/_internal/web.txt": b"new page"}).items():
                archive.writestr(name, content)
        self.github.publish("3.2.1", {"portable": buffer.getvalue()})
        return self.updater(kind="portable", app_dir=app)

    def run_swap(self):
        """Run the launched swap as it would run once this copy has closed (an ended process stands in for it), without
        starting the app afterwards."""
        command, env = self.launched[-1], dict(self.envs[-1] or {})
        gone = subprocess.Popen(["cmd.exe", "/c", "exit"], creationflags=updater.NO_WINDOW)
        gone.wait()
        env.update(PPN_PID=str(gone.pid), PPN_RESTART="0")
        return subprocess.run(command, capture_output=True, timeout=60, env={**os.environ, **env},
                              creationflags=updater.NO_WINDOW)

    def test_automatic_download_check_prompt_and_launch_of_the_installer(self):
        installer = os.urandom(4096)
        self.github.publish("3.2.1", {"installer": installer, "portable": b"zip"})
        up = self.updater()
        view = up.check(manual=False)
        self.assertEqual(view["state"], "ready", view)
        self.assertEqual(view["version"], "3.2.1")
        downloaded = self.folder / "data" / "updates" / "PrivatePartyNetSetup-3.2.1.exe"
        self.assertEqual(downloaded.read_bytes(), installer)
        self.assertEqual(up.restart_now()["ok"], True)
        (command,) = self.launched
        self.assertEqual(command[0], str(downloaded))
        self.assertEqual(command[1:], ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"])
        self.assertEqual(self.quits, [1], "the app closes so the installer can replace it")

    def test_a_download_that_is_not_the_file_github_published_is_refused(self):
        self.github.publish("3.2.1", {"installer": b"the real one"})
        self.github.files["/assets/PrivatePartyNetSetup-3.2.1.exe"] = b"the evil one"  # same size, other bytes
        view = self.updater().check(manual=False)
        self.assertEqual(view["state"], "error")
        self.assertIn("SHA-256", view["message"])
        self.assertFalse((self.folder / "data" / "updates" / "PrivatePartyNetSetup-3.2.1.exe").exists())

    def test_a_release_without_githubs_hashes_is_not_offered(self):
        self.github.publish("3.2.1", {"installer": b"x"}, digests=False)
        view = self.updater().check(manual=True)
        self.assertEqual(view["state"], "error")
        self.assertIn("no size or SHA-256", view["message"])

    def test_a_release_needs_no_signature(self):
        """K, 2026-11-05: whatever release is newest on GitHub. Nothing but the installer is published here."""
        self.github.publish("3.2.1", {"installer": b"just the installer"})
        self.assertEqual(self.updater().check(manual=False)["state"], "ready")

    def test_a_download_redirected_to_another_host_is_refused(self):
        self.github.publish("3.2.1", {"installer": b"x" * 10})
        self.github.redirect["/assets/PrivatePartyNetSetup-3.2.1.exe"] = "https://evil.example/installer.exe"
        self.github.files.pop("/assets/PrivatePartyNetSetup-3.2.1.exe")
        view = self.updater().check(manual=False)
        self.assertEqual(view["state"], "error")
        self.assertIn("evil.example", view["message"])

    def test_the_installer_is_hashed_again_just_before_it_runs(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        up.check(manual=False)
        up.ready.write_bytes(b"y" * 100)  # changed on disk after it was checked
        answer = up.restart_now()
        self.assertFalse(answer["ok"])
        self.assertIn("SHA-256", answer["message"])
        self.assertEqual(self.launched, [], "nothing was run")
        self.assertEqual(up.view()["state"], "error")

    def test_nothing_can_change_or_replace_the_installer_from_its_last_check_until_it_has_started(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        up.check(manual=False)
        tried = []

        def launch(command, env):
            path = Path(command[0])
            for attempt in (lambda: path.open("r+b").close(), lambda: path.rename(path.with_suffix(".old")),
                            lambda: path.unlink()):
                try:
                    attempt()
                    tried.append("allowed")
                except PermissionError:
                    tried.append("refused")
            self.assertEqual(path.read_bytes(), b"x" * 100, "it can still be read (and so started)")

        up._launch = launch
        self.assertTrue(up.restart_now()["ok"])
        self.assertEqual(tried, ["refused", "refused", "refused"])
        up.ready.open("r+b").close()  # let go once it has started

    def test_an_installer_that_is_gone_or_will_not_start_is_an_error_not_a_crash(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        up.check(manual=False)
        up.ready.unlink()  # removed by a virus scanner, say
        answer = up.restart_now()
        self.assertFalse(answer["ok"])
        self.assertEqual(self.quits, [])
        up.on_exit()  # and the app still closes cleanly

        def refuse(command, env):
            raise PermissionError(5, "Access is denied")

        up = self.updater()
        up.check(manual=False)
        up._launch = refuse
        answer = up.restart_now()
        self.assertFalse(answer["ok"])
        self.assertIn("denied", answer["message"])
        self.assertEqual(self.quits, [], "the app stays open when the installer did not start")
        self.assertEqual(up.view()["state"], "error")

    def test_an_update_waits_for_a_party_a_call_or_the_game_network(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        self.busy = "the game network"
        view = up.check(manual=False)
        self.assertEqual((view["state"], view["waiting"]), ("ready", "the game network"))
        answer = up.restart_now()
        self.assertFalse(answer["ok"])
        self.assertIn("the game network", answer["message"])
        self.assertEqual(self.launched, [])
        self.assertNotIn("installs when the game network ends", up.view()["message"])
        self.assertIn("installs when you close PrivatePartyNet", up.view()["message"])
        self.assertNotIn("installs then", answer["message"])
        self.busy = ""
        up.busy_changed()
        self.assertEqual(up.view()["waiting"], "")
        self.assertEqual(up.view()["message"],
                         "Ready: Restart now, or PrivatePartyNet 3.2.1 installs when you close PrivatePartyNet.")
        self.assertEqual(self.launched, [], "the party ending does not restart the app")
        self.assertTrue(up.restart_now()["ok"])
        self.assertEqual(len(self.launched), 1)

    def test_later_installs_when_the_app_closes_without_starting_it_again(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        up.check(manual=False)
        up.later()
        self.assertEqual(self.launched, [])
        up.on_exit()
        (command,) = self.launched
        self.assertIn("/NOLAUNCH=1", command)
        self.assertEqual(self.quits, [], "the app is already closing")

    def test_with_automatic_updates_off_a_check_offers_update_later_and_skip(self):
        self.settings["auto_update"] = False
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        view = up.check(manual=True)
        self.assertEqual(view["state"], "available")
        self.assertFalse((self.folder / "data" / "updates" / "PrivatePartyNetSetup-3.2.1.exe").exists(), "not fetched yet")
        up.skip()
        self.assertEqual(self.settings["update_skip"], "3.2.1")
        self.assertEqual(up.check(manual=True)["state"], "current", "a skipped version is not offered again")

    def test_update_from_the_offer_downloads_checks_and_installs(self):
        self.settings["auto_update"] = False
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        up.check(manual=True)
        self.assertTrue(up.restart_now()["ok"])
        self.assertEqual(len(self.launched), 1)

    def test_test_versions_follow_the_switch(self):
        self.github.publish("3.3.0-alpha.1", {"installer": b"x" * 10}, prerelease=True)
        self.settings["update_prerelease"] = False
        self.assertEqual(self.updater().check(manual=True)["state"], "current")
        self.settings["update_prerelease"] = True
        self.assertEqual(self.updater().check(manual=True)["version"], "3.3.0-alpha.1")

    def test_checks_send_the_etag_and_back_off_when_github_says_so(self):
        self.github.publish("3.2.1", {"installer": b"x" * 10})
        up = self.updater()
        up.check(manual=True)
        up.join_download()
        up.check(manual=True)
        listing = [etag for path, etag in self.github.calls if path.startswith("/releases")]
        self.assertIsNone(listing[0])
        self.assertTrue(listing[1], "the second check asks only for what changed")
        self.github.status = 429
        self.assertEqual(up.check(manual=True)["state"], "error")
        self.assertGreaterEqual(up._memory["backoff_until"] - time.time(), 3500)
        calls = len(self.github.calls)
        up.check(manual=False)  # an automatic check waits out the back-off
        self.assertEqual(len(self.github.calls), calls)
        until = up._memory["backoff_until"]
        for _ in range(3):  # and so does the button, however often it is pressed
            view = up.check(manual=True)
        self.assertEqual(len(self.github.calls), calls, "GitHub is not asked again")
        self.assertEqual(up._memory["backoff_until"], until, "the back-off does not grow")
        self.assertEqual(view["state"], "error")
        self.assertRegex(view["message"], r"^GitHub asked us to wait; try again after \d\d:\d\d( tomorrow)?\.$")

    def test_a_malformed_answer_from_github_is_an_error_not_a_crash(self):
        self.github.publish("3.2.1", {"installer": b"x" * 10})
        good = self.github.releases[0]
        for answer in (5, [dict(good, assets=7)], [dict(good, assets=[dict(a, size="big") for a in good["assets"]])],
                       [dict(good, assets=[dict(a, size=[1]) for a in good["assets"]])]):
            with self.subTest(answer=answer):
                self.github.releases = answer
                self.assertEqual(self.updater().check(manual=True)["state"], "error")

    def test_a_damaged_state_file_is_set_aside(self):
        self.github.publish("3.2.1", {"installer": b"x" * 10})
        updates = self.folder / "data" / "updates"
        updates.mkdir(parents=True)
        (updates / "state.json").write_text(json.dumps({"backoff_until": "soon", "backoff": [], "checked_at": "x",
                                                        "key_links": 5, "etag": 7, "releases": "x"}))
        up = self.updater()
        self.assertEqual(up.check(manual=False)["state"], "ready")
        self.assertIsInstance(up.view()["checked_at"], float)
        for until in (float("inf"), 1e300):
            (updates / "state.json").write_text(json.dumps({"backoff_until": until}))
            self.assertLessEqual(self.updater()._memory.get("backoff_until", 0), time.time() + updater.MAX_BACKOFF,
                                 "a damaged file cannot stop the checks for good")

    def test_two_checks_at_once_ask_and_download_once(self):
        self.github.publish("3.2.1", {"installer": b"x" * 100})
        up = self.updater()
        other = []

        def clock():  # a second check (the scheduler, say) arrives while the first is under way
            if not other:
                other.append(threading.Thread(target=up.check, kwargs={"manual": False}))
                other[0].start()
                other[0].join()
            return time.time()

        up._clock = clock
        up.check(manual=True)
        up.join_download()
        self.assertEqual(up.view()["state"], "ready")
        fetched = [path for path, _ in self.github.calls]
        self.assertEqual(fetched.count("/releases"), 1, fetched)
        self.assertEqual(fetched.count("/assets/PrivatePartyNetSetup-3.2.1.exe"), 1, fetched)

    def test_a_manual_check_returns_at_once_and_the_download_shows_its_progress(self):
        installer = os.urandom(300 * 1024)
        self.github.publish("3.2.1", {"installer": installer})
        up = self.updater()
        go, seen = threading.Event(), []
        plain = up.fetcher.get

        def get(url, hosts, headers=None, limit=updater.MAX_MANIFEST, into=None, progress=None):
            if into is None:
                return plain(url, hosts, headers, limit)

            def step(done):
                seen.append(up.view()["message"])
                progress(done)
                if done >= 64 * 1024:
                    go.wait(10)  # the download stays under way until the test has looked

            return plain(url, hosts, headers, limit, into=into, progress=step)

        up.fetcher.get = get
        view = up.check(manual=True)
        self.assertEqual(view["state"], "downloading", "the button's call returns once the check is done")
        for _ in range(100):
            if any("%" in m for m in seen):
                break
            time.sleep(0.05)
        self.assertRegex(up.view()["message"], r"^Downloading PrivatePartyNet 3\.2\.1\.\.\. \d+%$")
        self.assertGreater(up.view()["progress"], 0)
        go.set()
        up.join_download()
        self.assertEqual(up.view()["state"], "ready")

    def test_the_schedule_keeps_going_when_a_check_fails_unexpectedly(self):
        up = self.updater()
        calls = []

        def check(manual):
            calls.append(manual)
            if len(calls) == 1:
                raise RuntimeError("something nobody foresaw")
            up.stop()

        up.check = check
        with unittest.mock.patch.object(updater, "FIRST_CHECK", (0, 0)), \
                unittest.mock.patch.object(updater, "CHECK_SECONDS", 0):
            up._run()
        self.assertEqual(calls, [False, False])

    def test_a_retry_after_in_lower_case_is_heeded_too(self):
        self.github.status = 403
        self.github.wait_header = ("retry-after", "90000")  # longer than the longest back-off
        up = self.updater()
        up.check(manual=True)
        self.assertGreaterEqual(up._memory["backoff_until"] - time.time(), updater.MAX_BACKOFF - 60)

    def test_old_downloads_are_deleted(self):
        updates = self.folder / "data" / "updates"
        updates.mkdir(parents=True)
        (updates / "PrivatePartyNetSetup-3.1.0.exe").write_bytes(b"old")
        self.github.publish("3.2.1", {"installer": b"x" * 10})
        self.updater().check(manual=False)
        self.assertEqual(sorted(p.name for p in updates.iterdir()), ["PrivatePartyNetSetup-3.2.1.exe", "state.json"])

    def test_a_portable_copy_unpacks_beside_itself_and_swaps_once_closed(self):
        app = self.folder / "portable" / "PrivatePartyNet"
        up = self.portable(app)
        self.assertEqual(up.check(manual=False)["state"], "ready")
        self.assertEqual((up.ready / "PrivatePartyNet.exe").read_bytes(), b"new app")
        self.assertEqual(up.ready.parent.parent, app.parent, "unpacked beside the app's own folder")
        self.assertTrue(up.restart_now()["ok"])
        (command,) = self.launched
        self.assertEqual(command[:2], ["cmd.exe", "/c"])
        run = self.run_swap()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((app / "PrivatePartyNet.exe").read_bytes(), b"new app")
        self.assertEqual((app / "_internal" / "web.txt").read_bytes(), b"new page")
        self.assertEqual((app.parent / "PrivatePartyNet.old" / "PrivatePartyNet.exe").read_bytes(), b"old app")

    def test_a_portable_copy_in_a_folder_with_accents_or_a_percent_sign_swaps_too(self):
        app = self.folder / "José's 100%games" / "PrivatePartyNet"
        up = self.portable(app)
        self.assertEqual(up.check(manual=False)["state"], "ready")
        self.assertTrue(up.restart_now()["ok"])
        run = self.run_swap()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((app / "PrivatePartyNet.exe").read_bytes(), b"new app")
        self.assertEqual((app.parent / "PrivatePartyNet.old" / "PrivatePartyNet.exe").read_bytes(), b"old app")

    def test_the_unpacked_portable_copy_is_checked_again_just_before_the_swap(self):
        for change in ("changed", "added", "removed"):
            with self.subTest(change=change):
                self.launched.clear()
                app = self.folder / change / "PrivatePartyNet"
                up = self.portable(app)
                up.check(manual=False)
                if change == "changed":
                    (up.ready / "PrivatePartyNet.exe").write_bytes(b"evil app")
                elif change == "added":
                    (up.ready / "_internal" / "version.dll").write_bytes(b"planted")
                else:
                    (up.ready / "_internal" / "web.txt").unlink()
                answer = up.restart_now()
                self.assertFalse(answer["ok"], answer)
                self.assertIn("changed", answer["message"])
                self.assertEqual(self.launched, [], "nothing was swapped in")
                self.assertEqual(up.view()["state"], "error")

    def test_the_portable_swap_waits_for_the_app_whatever_find_is_first_on_the_path(self):
        app = self.folder / "portable" / "PrivatePartyNet"
        up = self.portable(app)
        up.check(manual=False)
        self.assertTrue(up.restart_now()["ok"])
        tools = self.folder / "tools"  # a Unix find (Git's, say) ahead of Windows' own
        tools.mkdir()
        (tools / "find.bat").write_text("@exit /b 1\n", encoding="ascii")
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"], creationflags=updater.NO_WINDOW)
        self.addCleanup(running.kill)
        env = {**os.environ, **self.envs[-1], "PPN_PID": str(running.pid), "PPN_RESTART": "0",
               "PATH": f"{tools};{os.environ.get('PATH', '')}"}
        swap = subprocess.Popen(self.launched[-1], env=env, creationflags=updater.NO_WINDOW,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self.assertEqual((app / "PrivatePartyNet.exe").read_bytes(), b"old app", "not swapped while the app runs")
        running.wait()
        self.assertEqual(swap.wait(timeout=60), 0)
        self.assertEqual((app / "PrivatePartyNet.exe").read_bytes(), b"new app")

    def test_stale_unpacked_versions_beside_a_portable_copy_are_cleaned_and_nothing_else(self):
        app = self.folder / "portable" / "PrivatePartyNet"
        up = self.portable(app)
        up.check(manual=False)
        self.assertTrue((up.ready.parent / updater.STAGING_MARK).is_file(), "the updater marks the folders it makes")
        parent = app.parent

        def folder(name, marked=True):
            path = parent / name
            (path / "PrivatePartyNet").mkdir(parents=True)
            if marked:
                (path / updater.STAGING_MARK).write_text("made by the updater", encoding="utf-8")
            return path

        stale = [folder("PrivatePartyNet.update-3.1.9"), folder("PrivatePartyNet.update-3.2.0-beta.2")]
        kept = [folder("PrivatePartyNet.update-3.1.8", marked=False), folder("PrivatePartyNet.update-old"),
                folder("PrivatePartyNet.update-3.1.9 (copy)"), folder("Other.update-3.1.9"),
                folder("PrivatePartyNet.update-3.1.9.bak")]
        target = folder("somewhere-else")
        junction = parent / "PrivatePartyNet.update-3.1.7"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True,
                       creationflags=updater.NO_WINDOW)
        up.clean()
        for path in stale:
            self.assertFalse(path.exists(), path.name)
        for path in kept + [target, up.ready.parent]:
            self.assertTrue(path.exists(), path.name)
        self.assertTrue((target / updater.STAGING_MARK).exists(), "a junction is never followed")

    def test_a_portable_copy_whose_folder_cannot_change_unpacks_into_updates_and_says_so(self):
        app = self.folder / "portable" / "PrivatePartyNet"
        app.mkdir(parents=True)
        (app.parent / "PrivatePartyNet.update-3.2.1").write_bytes(b"in the way")  # unpacking beside it fails
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("PrivatePartyNet/PrivatePartyNet.exe", b"new app")
        self.github.publish("3.2.1", {"portable": buffer.getvalue()})
        up = self.updater(kind="portable", app_dir=app)
        view = up.check(manual=False)
        self.assertEqual(view["state"], "manual", view)
        self.assertEqual((up.ready / "PrivatePartyNet.exe").read_bytes(), b"new app", "kept, not cleaned away")
        self.assertIn(self.folder / "data" / "updates", up.ready.parents)
        up.on_exit()
        self.assertEqual(self.launched, [], "no swap is tried for a folder that cannot change")

    def test_a_portable_zip_that_names_files_outside_its_folder_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../escaped.txt", b"x")
        self.github.publish("3.2.1", {"portable": buffer.getvalue()})
        view = self.updater(kind="portable").check(manual=False)
        self.assertEqual(view["state"], "error")
        self.assertIn("outside", view["message"])
        self.assertFalse((self.folder / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
