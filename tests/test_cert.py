import ssl
import tempfile
import unittest
from pathlib import Path

from cryptography import x509

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

    def test_replaces_the_certificate_when_a_new_address_appears(self):
        first = ensure_certificate(self.folder, ["127.0.0.1"])[0].read_bytes()
        certfile, _ = ensure_certificate(self.folder, ["127.0.0.1", "10.0.0.5"])
        self.assertIn("10.0.0.5", covered_names(certfile))
        self.assertNotEqual(first, certfile.read_bytes())

    def test_the_pair_loads_into_a_tls_server_context(self):
        certfile, keyfile = ensure_certificate(self.folder, ["127.0.0.1"])
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certfile, keyfile)


if __name__ == "__main__":
    unittest.main()
