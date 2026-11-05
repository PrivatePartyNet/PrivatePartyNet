import datetime
import hashlib
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509

from privatepartynet import cert
from privatepartynet.cert import ensure_certificate


def covered_names(cert_path):
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    addresses = {str(address) for address in san.get_values_for_type(x509.IPAddress)}
    return addresses | set(san.get_values_for_type(x509.DNSName))


class CertificateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def test_covers_every_host_it_is_asked_for(self):
        certfile, _ = ensure_certificate(self.folder, ["localhost", "127.0.0.1", "192.168.1.23"])
        self.assertEqual(covered_names(certfile), {"localhost", "127.0.0.1", "192.168.1.23"})

    def test_reuses_the_saved_certificate_while_it_still_covers_the_hosts(self):
        first = ensure_certificate(self.folder, ["127.0.0.1"])[0].read_bytes()
        second = ensure_certificate(self.folder, ["127.0.0.1"])[0].read_bytes()
        self.assertEqual(first, second)

    def test_keeps_the_certificate_when_this_pcs_addresses_change(self):
        # Every join checks the certificate's fingerprint, never its names, and a new certificate would break every
        # pin already out there: remembered parties, invites sent, game network links.
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1", "192.168.1.95"])
        first, key = certfile.read_bytes(), keyfile.read_bytes()
        for hosts in (["127.0.0.1", "192.168.1.95", "26.119.215.183"], ["127.0.0.1", "10.0.0.5"]):
            certfile, keyfile = ensure_certificate(self.folder, hosts)
            self.assertEqual(certfile.read_bytes(), first, hosts)
            self.assertEqual(keyfile.read_bytes(), key)

    def test_a_certificate_near_its_end_is_renewed_with_the_same_key(self):
        with mock.patch.object(cert, "VALID_FOR", datetime.timedelta(days=10)):  # inside RENEW_BEFORE already
            certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1"])
        old_pem, key = certfile.read_text(encoding="utf-8"), keyfile.read_bytes()
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1", "10.0.0.5"])
        new_pem = certfile.read_text(encoding="utf-8")
        self.assertNotEqual(new_pem, old_pem, "renewed")
        self.assertEqual(keyfile.read_bytes(), key, "with the same key")
        self.assertEqual(cert.pins_of_pem(new_pem)[1], cert.pins_of_pem(old_pem)[1], "so its key pin is the same")
        self.assertIn("10.0.0.5", covered_names(certfile), "and it names the addresses of the day")

    def test_a_damaged_certificate_is_made_again_with_the_same_key(self):
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1"])
        spki, key = cert.pins_of_pem(certfile.read_text(encoding="utf-8"))[1], keyfile.read_bytes()
        certfile.write_text("not a certificate", encoding="utf-8")
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1"])
        self.assertEqual(keyfile.read_bytes(), key)
        self.assertEqual(cert.pins_of_pem(certfile.read_text(encoding="utf-8"))[1], spki)

    def test_old_whole_certificate_pins_and_new_key_pins_both_match(self):
        certfile, _ = ensure_certificate(self.folder, ["127.0.0.1"])
        pem = certfile.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
        whole, spki = cert.pins_of_pem(pem)
        self.assertEqual(whole, hashlib.sha256(der).hexdigest(), "what invites and 3.0/3.1 peers pin")
        self.assertNotEqual(spki, whole)
        self.assertTrue(cert.matches(whole, der))
        self.assertTrue(cert.matches(spki, der))
        self.assertTrue(cert.matches(("", spki), der))
        self.assertFalse(cert.matches("0" * 64, der))
        self.assertFalse(cert.matches("", der), "no pin matches nothing")

    def test_the_pair_loads_into_a_tls_server_context(self):
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1"])
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certfile, keyfile)


if __name__ == "__main__":
    unittest.main()
