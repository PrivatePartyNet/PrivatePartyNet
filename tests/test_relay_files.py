"""The relay's deployment files agree with each other and with the server, and run on Linux as they are."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from privatepartynet import relayproto as rp

RELAY = Path(__file__).resolve().parents[1] / "relay"
LINUX_FILES = ["setup-ubuntu.sh", "docker-entrypoint.sh", "turnserver.conf", "docker-compose.yml", "Dockerfile",
               "systemd/ppn-relay.service", "systemd/coturn-ppn.conf"]


class RelayFileTests(unittest.TestCase):
    def read(self, name):
        return (RELAY / name).read_text(encoding="utf-8")

    def test_linux_files_have_unix_line_endings(self):
        for name in LINUX_FILES:
            self.assertNotIn(b"\r", (RELAY / name).read_bytes(), name)

    def test_coturn_takes_credentials_made_from_the_secret_and_no_placeholder_secret(self):
        text = self.read("turnserver.conf")
        self.assertIn("\nuse-auth-secret\n", text)
        self.assertNotRegex(text, r"(?m)^static-auth-secret")  # added to the installed copy only, never a fixed one
        for name in ("setup-ubuntu.sh", "docker-compose.yml"):
            self.assertIn("static-auth-secret=$", self.read(name), name)

    def test_coturn_does_not_relay_to_private_networks(self):
        text = self.read("turnserver.conf")
        for start in ("10.0.0.0", "127.0.0.0", "169.254.0.0", "172.16.0.0", "192.168.0.0", "100.64.0.0"):
            self.assertIn(f"denied-peer-ip={start}-", text)

    def test_the_media_ports_agree_everywhere(self):
        text = self.read("turnserver.conf")
        low, high = re.search(r"min-port=(\d+)", text).group(1), re.search(r"max-port=(\d+)", text).group(1)
        self.assertIn(f"{low}:{high}/udp", self.read("setup-ubuntu.sh"))
        self.assertIn(f"{low}-{high}", self.read("README.md"))

    def test_the_relay_ports_agree_with_the_server(self):
        for name in ("docker-compose.yml", "Dockerfile", "README.md"):
            self.assertIn(str(rp.DEFAULT_UDP_PORT), self.read(name), name)
        self.assertIn(f"UDP_PORT={rp.DEFAULT_UDP_PORT}", self.read("systemd/ppn-relay.service"))

    def test_the_service_runs_what_the_installer_installs(self):
        unit, setup = self.read("systemd/ppn-relay.service"), self.read("setup-ubuntu.sh")
        self.assertIn("/opt/ppn-relay/ppn_relay.py", unit)
        self.assertIn("/opt/ppn-relay/ppn_relay.py", setup)
        self.assertIn("privatepartynet/relayproto.py", setup)
        self.assertIn("User=ppn-relay", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", unit)

    def test_the_image_copies_files_that_exist(self):
        root = RELAY.parent
        for line in self.read("Dockerfile").splitlines():
            if line.startswith("COPY "):
                for source in line.split()[1:-1]:
                    self.assertTrue((root / source).exists(), source)

    @unittest.skipUnless(shutil.which("sh"), "no POSIX shell on this PC")
    def test_the_scripts_parse(self):
        for name in ("setup-ubuntu.sh", "docker-entrypoint.sh"):
            run = subprocess.run([shutil.which("sh"), "-n", str(RELAY / name)], capture_output=True, text=True,
                                 timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(run.returncode, 0, name + run.stderr)


if __name__ == "__main__":
    unittest.main()
