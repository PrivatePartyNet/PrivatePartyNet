"""Updates: version order, the signed manifest's checks, key rotation, waiting for a party, and the whole flow against
a fake GitHub on this PC (HTTPS, with a test key passed in by code, as only tests can)."""

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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from privatepartynet import updater
from privatepartynet.cert import ensure_certificate
from privatepartynet.updater import (MANIFEST_LABEL, NEXT_KEY_LABEL, Fetcher, UpdateError, Updater, Version, candidates,
                                     check_manifest, manifest_name)

KEY = Ed25519PrivateKey.generate()
PUBLIC = KEY.public_key().public_bytes_raw().hex()


def manifest(version, files, key=KEY, tag=None, next_key=None, notes="Fixes."):
    record = {"app": "PrivatePartyNet", "version": version, "tag": tag or "v" + version, "files": files, "notes": notes}
    if next_key is not None:
        record["next_key"] = next_key
    data = json.dumps(record).encode()
    return data, key.sign(MANIFEST_LABEL + data).hex().encode()


def entry(version, kind, content):
    name = updater.file_names(version)[kind]
    return {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "kind": kind}


def release(version, prerelease=False, draft=False, assets=()):
    return {"tag_name": "v" + version, "prerelease": prerelease, "draft": draft, "body": "notes",
            "assets": [{"name": n, "size": s, "browser_download_url": u} for n, s, u in assets]}


class VersionTests(unittest.TestCase):
    def test_versions_sort_as_semantic_versioning_says_tags_included(self):
        order = ["3.1.0", "v3.2.0-alpha.1", "3.2.0-alpha.2", "3.2.0-alpha.10", "3.2.0-beta", "3.2.0-rc.1", "v3.2.0",
                 "3.2.1", "3.10.0"]
        versions = [Version(v) for v in order]
        self.assertEqual(versions, sorted(reversed(versions)))
        self.assertEqual(Version("v3.2.0"), Version("3.2.0"))
        self.assertEqual(Version("3.2.0+build.7"), Version("3.2.0"))
        self.assertTrue(Version("3.2.0-alpha.1").prerelease)
        for bad in ("3.2", "version 3", "3.2.0.1", ""):
            self.assertIsNone(updater.parse_version(bad))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.content = b"the installer"
        self.files = [entry("3.2.1", "installer", self.content)]

    def check(self, data, sig, version="3.2.1", current="3.2.0", keys=(PUBLIC,)):
        return check_manifest(data, sig, keys, release(version), current)

    def test_a_good_manifest_is_accepted(self):
        found = self.check(*manifest("3.2.1", self.files))
        self.assertEqual(str(found["version"]), "3.2.1")
        self.assertEqual(found["files"]["installer"]["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(found["notes"], "Fixes.")

    def test_a_bad_signature_is_refused(self):
        data, _ = manifest("3.2.1", self.files)
        with self.assertRaisesRegex(UpdateError, "signature"):
            self.check(data, Ed25519PrivateKey.generate().sign(MANIFEST_LABEL + data).hex().encode())
        with self.assertRaisesRegex(UpdateError, "malformed"):
            self.check(data, b"not hex")

    def test_a_tampered_hash_or_size_breaks_the_signature(self):
        data, sig = manifest("3.2.1", self.files)
        for change in (b'"size": 13', self.files[0]["sha256"].encode()):
            tampered = data.replace(change, change[:-1] + (b"4" if change[-1:] != b"4" else b"5"))
            self.assertNotEqual(tampered, data)
            with self.assertRaisesRegex(UpdateError, "signature"):
                self.check(tampered, sig)

    def test_no_rollback_and_no_same_version(self):
        for current in ("3.2.1", "3.3.0"):
            with self.subTest(current=current):
                with self.assertRaisesRegex(UpdateError, "not newer"):
                    self.check(*manifest("3.2.1", self.files), current=current)

    def test_the_manifests_version_and_tag_must_be_the_releases(self):
        with self.assertRaisesRegex(UpdateError, "version and tag"):
            self.check(*manifest("3.2.1", self.files), version="3.2.2")  # a manifest moved to another release
        with self.assertRaisesRegex(UpdateError, "version and tag"):
            self.check(*manifest("3.2.1", self.files, tag="v3.2.2"))

    def test_a_file_name_that_is_not_the_versions_own_is_refused(self):
        files = [dict(self.files[0], name="evil.exe")]
        with self.assertRaisesRegex(UpdateError, "not one this app accepts"):
            self.check(*manifest("3.2.1", files))

    def test_release_notes_are_plain_text_and_short(self):
        found = self.check(*manifest("3.2.1", self.files, notes="<b>bold</b>\x1b[31m" + "x" * 5000))
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


class KeyRotationTests(unittest.TestCase):
    def test_a_next_key_signed_by_the_current_one_is_learned_and_then_trusted(self):
        new = Ed25519PrivateKey.generate()
        new_public = new.public_key().public_bytes_raw().hex()
        files = [entry("3.2.1", "installer", b"x")]
        hand_on = {"key": new_public, "sig": KEY.sign(NEXT_KEY_LABEL + bytes.fromhex(new_public)).hex()}
        found = check_manifest(*manifest("3.2.1", files, next_key=hand_on), (PUBLIC,), release("3.2.1"), "3.2.0")
        self.assertEqual(found["next_key"], new_public)
        later = manifest("3.3.0", [entry("3.3.0", "installer", b"y")], key=new)
        with self.assertRaises(UpdateError):
            check_manifest(*later, (PUBLIC,), release("3.3.0"), "3.2.1")  # not before it was learned
        check_manifest(*later, (PUBLIC, new_public), release("3.3.0"), "3.2.1")

    def test_a_next_key_not_signed_by_the_current_one_is_refused(self):
        rogue = Ed25519PrivateKey.generate()
        rogue_public = rogue.public_key().public_bytes_raw().hex()
        hand_on = {"key": rogue_public, "sig": rogue.sign(NEXT_KEY_LABEL + bytes.fromhex(rogue_public)).hex()}
        with self.assertRaisesRegex(UpdateError, "next key"):
            check_manifest(*manifest("3.2.1", [entry("3.2.1", "installer", b"x")], next_key=hand_on), (PUBLIC,),
                           release("3.2.1"), "3.2.0")


class LearnedKeyTests(unittest.TestCase):
    """Keys learned by rotation are kept in the data folder, so they must prove themselves again each time."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        (self.data / "updates").mkdir()

    def updater(self, keys=(PUBLIC,)):
        return Updater(dict, lambda **c: None, self.data, lambda: "", current="3.2.0", kind="installed", keys=keys,
                       fetcher=object(), launcher=lambda *a: None, app_dir=self.data / "app")

    def state(self, **record):
        (self.data / "updates" / "state.json").write_text(json.dumps(record), encoding="utf-8")

    def test_a_key_written_into_the_state_file_is_not_trusted(self):
        rogue = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        self.state(keys=[rogue])
        self.assertEqual(self.updater().keys(), (PUBLIC,))
        self.state(key_links=[{"key": rogue, "by": PUBLIC, "sig": "00" * 64}])
        self.assertEqual(self.updater().keys(), (PUBLIC,))

    def test_a_learned_key_is_trusted_again_after_a_restart(self):
        new = Ed25519PrivateKey.generate()
        new_public = new.public_key().public_bytes_raw().hex()
        newer_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        links = [{"key": new_public, "by": PUBLIC, "sig": KEY.sign(NEXT_KEY_LABEL + bytes.fromhex(new_public)).hex()},
                 {"key": newer_public, "by": new_public,
                  "sig": new.sign(NEXT_KEY_LABEL + bytes.fromhex(newer_public)).hex()}]
        self.state(key_links=list(reversed(links)))  # in any order
        self.assertEqual(self.updater().keys(), (PUBLIC, new_public, newer_public))

    def test_keys_learned_from_a_key_the_app_no_longer_carries_are_dropped_with_it(self):
        leaked = Ed25519PrivateKey.generate()
        leaked_public = leaked.public_key().public_bytes_raw().hex()
        planted = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        self.state(key_links=[{"key": planted, "by": leaked_public,
                               "sig": leaked.sign(NEXT_KEY_LABEL + bytes.fromhex(planted)).hex()}])
        self.assertIn(planted, self.updater(keys=(leaked_public,)).keys())
        self.assertEqual(self.updater(keys=(PUBLIC,)).keys(), (PUBLIC,), "a build without the leaked key")

    def test_a_key_handed_on_by_a_release_is_kept_with_its_proof(self):
        new = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        hand_on = {"key": new, "sig": KEY.sign(NEXT_KEY_LABEL + bytes.fromhex(new)).hex()}
        data, sig = manifest("3.2.1", [entry("3.2.1", "installer", b"x")], next_key=hand_on)
        up = self.updater()
        up.small = {"m": data, "s": sig}
        up._small = lambda item: up.small[item["browser_download_url"]]
        rel = release("3.2.1", assets=[(manifest_name("3.2.1"), len(data), "m"),
                                       (manifest_name("3.2.1") + ".sig", len(sig), "s"),
                                       ("PrivatePartyNetSetup-3.2.1.exe", 1, "https://github.com/x")])
        up._check_release(rel)
        self.assertEqual(self.updater().keys(), (PUBLIC, new), "trusted after a restart, from the state file")


class NoBackDoorTests(unittest.TestCase):
    def test_no_environment_variable_or_setting_changes_the_key_or_the_address(self):
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
        self.assertEqual(len(updater.TRUSTED_KEYS), 1)
        self.assertEqual(len(bytes.fromhex(updater.TRUSTED_KEYS[0])), 32)

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

    def publish(self, version, payloads, prerelease=False, key=KEY, sign=True):
        """A release of `version` whose files are `payloads` {kind: bytes}, with a signed manifest."""
        files = [entry(version, kind, content) for kind, content in payloads.items()]
        data, sig = manifest(version, files, key=key)
        assets = []
        for item in files:
            self.files[f"/assets/{item['name']}"] = payloads[item["kind"]]
            assets.append((item["name"], item["size"], f"{self.base}/assets/{item['name']}"))
        if sign:
            for name, body in ((manifest_name(version), data), (manifest_name(version) + ".sig", sig)):
                self.files[f"/assets/{name}"] = body
                assets.append((name, len(body), f"{self.base}/assets/{name}"))
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
        return Updater(lambda: self.settings, lambda **c: self.settings.update(c), self.folder / "data",
                       lambda: self.busy, current="3.2.0", kind=kind, api_url=f"{self.github.base}/releases",
                       keys=(PUBLIC,), api_hosts={"127.0.0.1"}, asset_hosts={"127.0.0.1"},
                       fetcher=Fetcher(self.github.client_context(), timeout=5), launcher=self.launch,
                       app_dir=app_dir or self.folder / "app" / "PrivatePartyNet", quit=lambda: self.quits.append(1))

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

    def test_a_download_that_is_not_the_signed_file_is_refused(self):
        self.github.publish("3.2.1", {"installer": b"the real one"})
        self.github.files["/assets/PrivatePartyNetSetup-3.2.1.exe"] = b"the evil one"  # same size, other bytes
        view = self.updater().check(manual=False)
        self.assertEqual(view["state"], "error")
        self.assertIn("SHA-256", view["message"])
        self.assertFalse((self.folder / "data" / "updates" / "PrivatePartyNetSetup-3.2.1.exe").exists())

    def test_a_release_without_a_signed_manifest_is_not_offered(self):
        self.github.publish("3.2.1", {"installer": b"x"}, sign=False)
        view = self.updater().check(manual=True)
        self.assertEqual(view["state"], "error")
        self.assertIn("no signed manifest", view["message"])

    def test_a_manifest_signed_by_another_key_is_refused(self):
        self.github.publish("3.2.1", {"installer": b"x"}, key=Ed25519PrivateKey.generate())
        self.assertIn("signature", self.updater().check(manual=True)["message"])

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
        self.busy = ""
        up.busy_changed()
        self.assertEqual(up.view()["waiting"], "")
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
        self.assertEqual(up.check(manual=True)["state"], "ready")
        fetched = [path for path, _ in self.github.calls]
        self.assertEqual(fetched.count("/releases"), 1, fetched)
        self.assertEqual(fetched.count("/assets/PrivatePartyNetSetup-3.2.1.exe"), 1, fetched)

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
