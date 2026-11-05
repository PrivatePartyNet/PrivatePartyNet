"""tools/sign_release.py: the manifest it writes is one the updater accepts, and it can hand on the next key."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_a_missing_file_stops_it(self):
        (self.dist / "PrivatePartyNetSetup-3.2.1.exe").unlink()
        with self.assertRaises(SystemExit):
            sign_release.sign("3.2.1", self.dist, key=self.key)

    def test_the_key_lives_outside_the_repository(self):
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn(str(root).lower(), str(sign_release.KEY_FILE).lower())
        self.assertEqual(sign_release.KEY_FILE.name, "update-signing.key")


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


if __name__ == "__main__":
    unittest.main()
