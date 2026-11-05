"""tools/sign_release.py: the manifest it writes is one the updater accepts, and it can hand on the next key."""

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from privatepartynet import updater

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sign_release  # noqa: E402


class SignReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dist = Path(temp.name)
        self.payloads = {"PrivatePartyNetSetup-3.2.1.exe": b"installer bytes",
                         "PrivatePartyNet-3.2.1-portable.zip": b"zip bytes"}
        for name, data in self.payloads.items():
            (self.dist / name).write_bytes(data)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes_raw().hex()

    def release(self, manifest):
        assets = [{"name": n, "size": len(d)} for n, d in self.payloads.items()]
        return {"tag_name": "v3.2.1", "assets": assets}

    def test_a_signed_release_passes_the_updaters_checks(self):
        manifest, signature = sign_release.sign("3.2.1", self.dist, key=self.key)
        self.assertEqual(manifest.name, "PrivatePartyNet-3.2.1-update.json")
        self.assertEqual(signature.name, "PrivatePartyNet-3.2.1-update.json.sig")
        found = updater.check_manifest(manifest.read_bytes(), signature.read_bytes(), (self.public,),
                                       self.release(manifest), "3.2.0")
        self.assertEqual(found["files"]["installer"]["sha256"],
                         hashlib.sha256(self.payloads["PrivatePartyNetSetup-3.2.1.exe"]).hexdigest())
        self.assertEqual(found["files"]["portable"]["size"], len(self.payloads["PrivatePartyNet-3.2.1-portable.zip"]))

    def test_the_next_key_is_signed_by_the_current_one(self):
        next_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        manifest, signature = sign_release.sign("3.2.1", self.dist, key=self.key, next_key=next_public)
        found = updater.check_manifest(manifest.read_bytes(), signature.read_bytes(), (self.public,),
                                       self.release(manifest), "3.2.0")
        self.assertEqual(found["next_key"], next_public)

    def test_keys_to_retire_are_each_signed_by_the_current_one(self):
        gone = [Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex() for _ in range(2)]
        manifest, signature = sign_release.sign("3.2.1", self.dist, key=self.key, revoke=gone)
        found = updater.check_manifest(manifest.read_bytes(), signature.read_bytes(), (self.public,),
                                       self.release(manifest), "3.2.0")
        self.assertEqual([r["key"] for r in found["revoke_links"]], gone)
        self.assertIn("--revoke", {o for action in sign_release.parser()._actions for o in action.option_strings})

    def test_a_missing_file_stops_it(self):
        (self.dist / "PrivatePartyNetSetup-3.2.1.exe").unlink()
        with self.assertRaises(SystemExit):
            sign_release.sign("3.2.1", self.dist, key=self.key)

    def test_the_key_lives_outside_the_repository(self):
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn(str(root).lower(), str(sign_release.KEY_FILE).lower())
        self.assertEqual(sign_release.KEY_FILE.name, "update-signing.key")


class KeyBackupTests(unittest.TestCase):
    """--export writes the signing key sealed with a passphrase, for a safe place off this PC; --import seals it into
    Windows data protection on this PC or another one."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = sign_release.public_hex(self.key)

    def export(self, passphrase="correct horse battery staple", log2_n=10):
        path = self.folder / "signing-key.ppnkey"
        sign_release.export_key(self.key, path, passphrase, log2_n=log2_n)
        return path

    def test_an_exported_key_comes_back_with_its_passphrase(self):
        path = self.export()
        text = path.read_text(encoding="utf-8")
        self.assertIn(self.public, text, "the public key is in the clear, to tell backups apart")
        self.assertNotIn(self.key.private_bytes_raw().hex(), text)
        back = sign_release.import_key(path, "correct horse battery staple")
        self.assertEqual(back.private_bytes_raw(), self.key.private_bytes_raw())

    def test_a_wrong_passphrase_or_a_changed_byte_is_refused(self):
        path = self.export()
        with self.assertRaisesRegex(SystemExit, "passphrase"):
            sign_release.import_key(path, "wrong horse battery staple")
        record = json.loads(path.read_text(encoding="utf-8"))
        for field in ("public", "salt", "sealed"):
            changed = dict(record)
            value = changed[field]
            changed[field] = ("0" if value[0] != "0" else "1") + value[1:]
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(SystemExit, msg=field):
                sign_release.import_key(path, "correct horse battery staple")

    def test_a_short_passphrase_an_existing_file_or_a_hostile_cost_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "12"):
            self.export(passphrase="short")
        self.export()
        with self.assertRaisesRegex(SystemExit, "already"):
            self.export()
        path = self.folder / "signing-key.ppnkey"
        record = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(dict(record, log2_n=40)), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "damaged"):
            sign_release.import_key(path, "correct horse battery staple")

    def test_import_seals_the_key_on_this_pc_and_never_replaces_another(self):
        path = self.export()
        target = self.folder / "release" / "update-signing.key"
        sealed = []
        with mock.patch.object(sign_release, "dpapi_protect", lambda data, entropy: sealed.append(data) or b"sealed"):
            sign_release.store_key(sign_release.import_key(path, "correct horse battery staple"), target)
        self.assertEqual(sealed, [self.key.private_bytes_raw()])
        self.assertEqual(target.read_bytes(), b"sealed")
        with self.assertRaisesRegex(SystemExit, "already"):
            sign_release.store_key(Ed25519PrivateKey.generate(), target)

    def test_the_passphrase_comes_only_from_a_prompt(self):
        options = {o for action in sign_release.parser()._actions for o in action.option_strings}
        self.assertIn("--export", options)
        self.assertIn("--import", options)
        self.assertFalse(any("pass" in o for o in options), "never on the command line")
        source = Path(sign_release.__file__).read_text(encoding="utf-8")
        self.assertIn("getpass.getpass(", source)
        self.assertNotIn("os.environ.get(\"PPN", source)
        asked = iter(["correct horse battery staple", "correct horse battery staple"])
        path = self.folder / "from-the-prompt.ppnkey"
        with mock.patch.object(sign_release, "load_key", lambda: self.key), \
                mock.patch.object(sign_release.getpass, "getpass", lambda prompt="": next(asked)), \
                mock.patch.object(sign_release, "EXPORT_LOG2_N", 10):
            self.assertEqual(sign_release.main(["--export", str(path)]), 0)
        self.assertEqual(sign_release.import_key(path, "correct horse battery staple").private_bytes_raw(),
                         self.key.private_bytes_raw())

    def test_the_docstring_names_only_options_that_exist(self):
        options = {o for action in sign_release.parser()._actions for o in action.option_strings}
        for named in re.findall(r"(?<![\w-])--[a-z][a-z-]+", sign_release.__doc__):
            self.assertIn(named, options, named)


class InstallerScriptTests(unittest.TestCase):
    def test_the_installer_waits_for_the_app_and_starts_it_again_after_an_update(self):
        text = (Path(__file__).resolve().parents[1] / "installer" / "PrivatePartyNet.iss").read_text(encoding="utf-8")
        mutex = text.split("AppMutex=")[1].split()[0]
        self.assertIn(f"CheckForMutexes('{mutex}')", text, "the same mutex the close-first check uses")
        self.assertIn("{param:UPDATE|0}", text)
        self.assertIn("{param:NOLAUNCH|0}", text)
        run = [line for line in text.splitlines() if line.startswith('Filename: "{app}\{#AppExe}"')][0]
        self.assertNotIn("skipifsilent", run, "a silent update starts the app again")
        self.assertIn("Check: LaunchAfter", run)

    def test_a_scripted_silent_install_is_told_how_not_to_start_the_app(self):
        root = Path(__file__).resolve().parents[1]
        contributing = (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertTrue("/SILENT /NOLAUNCH=1" in contributing, "CONTRIBUTING.md says how")
        iss = (root / "installer" / "PrivatePartyNet.iss").read_text(encoding="utf-8")
        comment = iss.split("[Run]", 1)[1].split('Filename: "{app}\\{#AppExe}"')[0]
        self.assertIn("/NOLAUNCH=1", comment)
        self.assertIn("scripted", comment)


if __name__ == "__main__":
    unittest.main()
