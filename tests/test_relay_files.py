"""The relay's deployment files agree with each other and with the server, and run on Linux as they are."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from privatepartynet import relayproto as rp

RELAY = Path(__file__).resolve().parents[1] / "relay"
LINUX_FILES = ["setup-ubuntu.sh", "docker-entrypoint.sh", "turnserver.conf", "docker-compose.yml", "Dockerfile",
               "systemd/ppn-relay.service", "systemd/coturn-ppn.conf", "turn-firewall.sh",
               "systemd/ppn-turn-firewall.service"]


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

    def test_coturn_does_not_relay_over_tcp_or_answer_strangers_questions(self):
        text = self.read("turnserver.conf")
        for flag in ("no-tcp-relay", "no-rfc5780", "no-stun-backward-compatibility", "no-software-attribute"):
            self.assertRegex(text, rf"(?m)^{flag}$")

    def test_docker_keeps_the_secret_in_a_file_not_the_environment(self):
        compose = self.read("docker-compose.yml")
        self.assertNotIn("PPN_RELAY_SECRET", compose)
        self.assertIn("file: ./secret", compose)
        self.assertIn("secret", self.read(".gitignore").split())

    def test_the_relay_and_coturn_serve_ipv6_too(self):
        setup, compose = self.read("setup-ubuntu.sh"), self.read("docker-compose.yml")
        self.assertIn("PUBLIC_IP6", setup)
        self.assertIn("allowed-peer-ip=$PUBLIC_IP6", setup)
        self.assertIn("PUBLIC_IP6", compose)
        self.assertNotIn("ports:", compose, "host networking, so members keep their real IPv6 addresses")
        self.assertNotIn("--host", self.read("systemd/ppn-relay.service"), "the server's own default: both families")

    def test_the_service_has_room_for_the_relay_at_its_worst(self):
        import sys

        sys.path.insert(0, str(RELAY))
        import ppn_relay

        unit = self.read("systemd/ppn-relay.service")
        self.assertIn("MemoryMax=512M", unit)
        self.assertIn("LimitNOFILE=8192", unit)
        self.assertEqual(ppn_relay.MAX_TCP, 512)
        measured = 400 * 1024  # bytes a TLS member costs at worst, by verify_memory.py (354-397 KiB here)
        self.assertLess(ppn_relay.MAX_TCP * measured + 64 * 2 ** 20, 512 * 2 ** 20)
        self.assertLess(ppn_relay.MAX_TCP * 2 + 64, 8192, "a socket for each, and room to spare")

    def test_setup_targets_ubuntu_24_04_and_refuses_an_older_python(self):
        setup = self.read("setup-ubuntu.sh")
        self.assertIn("Ubuntu 24.04", setup)
        self.assertIn("sys.version_info < (3, 12)", setup)
        self.assertIn("python:3.12", self.read("Dockerfile"))
        self.assertIn("24.04", self.read("README.md"))

    def test_coturn_is_kept_away_from_the_vps_itself(self):
        firewall, setup = self.read("turn-firewall.sh"), self.read("setup-ubuntu.sh")
        text = self.read("turnserver.conf")
        low, high = re.search(r"min-port=(\d+)", text).group(1), re.search(r"max-port=(\d+)", text).group(1)
        self.assertIn(f"MIN_PORT:-{low}", firewall)
        self.assertIn(f"MAX_PORT:-{high}", firewall)
        self.assertIn("-m owner --uid-owner", firewall)
        self.assertIn("apply iptables 4 127.0.0.0/8", firewall)
        self.assertIn("apply ip6tables 6 ::1/128", firewall)
        # Running it again replaces its rules: the chain is made or emptied, and the jump to it added only once.
        self.assertIn('-N "$CHAIN" 2>/dev/null || "$tool" -F "$CHAIN"', firewall)
        self.assertIn('-C OUTPUT -m owner --uid-owner "$TURN_USER" -j "$CHAIN"', firewall)
        # Kept across reboots, and in place before coturn relays anything.
        self.assertIn("ExecStart=/bin/sh /opt/ppn-relay/turn-firewall.sh", self.read("systemd/ppn-turn-firewall.service"))
        self.assertIn("Before=coturn.service", self.read("systemd/ppn-turn-firewall.service"))
        self.assertIn("/opt/ppn-relay/turn-firewall.sh", setup)
        self.assertLess(setup.index("restart ppn-turn-firewall"), setup.index("enable --now coturn"))

    def test_coturn_does_not_relay_to_private_networks(self):
        text = self.read("turnserver.conf")
        for start in ("10.0.0.0", "127.0.0.0", "169.254.0.0", "172.16.0.0", "192.168.0.0", "100.64.0.0", "192.0.0.0",
                      "198.18.0.0", "224.0.0.0", "::ffff:0.0.0.0", "64:ff9b::", "::", "fc00::", "fe80::"):
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
        for name in ("setup-ubuntu.sh", "docker-entrypoint.sh", "turn-firewall.sh"):
            run = subprocess.run([shutil.which("sh"), "-n", str(RELAY / name)], capture_output=True, text=True,
                                 timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(run.returncode, 0, name + run.stderr)


if __name__ == "__main__":
    unittest.main()
