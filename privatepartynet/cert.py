"""Creates the self-signed certificate that serves PrivatePartyNet over HTTPS.

Browsers allow screen and microphone capture only on a secure origin, and a LAN address is not one unless it
is served over HTTPS. The certificate is self-signed, so the browser warns once per certificate. It is kept in
the user's profile and reused until it runs out soon or stops covering the addresses in use.
"""

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_FILE = "privatepartynet-cert.pem"
KEY_FILE = "privatepartynet-key.pem"
VALID_FOR = datetime.timedelta(days=365)
RENEW_BEFORE = datetime.timedelta(days=30)


def ensure_certificate(directory, hosts):
    """Return (cert_path, key_path) for a certificate that covers every name and address in hosts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    if cert_path.exists() and key_path.exists() and _still_good(cert_path, hosts):
        return cert_path, key_path
    _create(cert_path, key_path, hosts)
    return cert_path, key_path


def _still_good(cert_path, hosts):
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return False
    if cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < RENEW_BEFORE:
        return False
    covered = {str(address) for address in san.get_values_for_type(x509.IPAddress)}
    covered |= set(san.get_values_for_type(x509.DNSName))
    return all(host in covered for host in hosts)


def _create(cert_path, key_path, hosts):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PrivatePartyNet")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + VALID_FOR)
        .add_extension(x509.SubjectAlternativeName([_name_for(host) for host in hosts]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _name_for(host):
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)
