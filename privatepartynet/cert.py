"""Creates the self-signed certificate that serves PrivatePartyNet over HTTPS.

Browsers allow screen and microphone capture only on a secure origin, and a LAN address is not one unless it
is served over HTTPS. The certificate is self-signed. It is kept in the user's profile and reused until it runs out
soon or is damaged, and then made again with the same key.

Nothing checks the names and addresses in it: every join pins the certificate instead. So it is not made again when
this PC's addresses change, which would break every pin already out there (remembered parties, invites sent, game
network links). A pin is either the SHA-256 of the whole certificate (what invites and 3.0/3.1 peers carry; it changes
when the certificate is renewed) or the SHA-256 of its public key (SubjectPublicKeyInfo), which a renewal keeps.
"""

import datetime
import hashlib
import ipaddress
import ssl
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
    """Return (cert_path, key_path). A certificate is made (naming `hosts`) only when there is none, it is damaged,
    or it runs out within RENEW_BEFORE; an existing key is always kept."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    key = _load_key(key_path)
    if key is not None and cert_path.exists() and _still_good(cert_path, key):
        return cert_path, key_path
    _create(cert_path, key_path, hosts, key)
    return cert_path, key_path


def _load_key(key_path):
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    return key if isinstance(key, rsa.RSAPrivateKey) else None


def _still_good(cert_path, key):
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return False
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        return False  # a certificate for another key is as good as damaged
    return cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) >= RENEW_BEFORE


def pins_of_der(der):
    """(SHA-256 of the whole certificate, SHA-256 of its public key), in hex, for a DER certificate."""
    public = x509.load_der_x509_certificate(der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest(), hashlib.sha256(public).hexdigest()


def pins_of_pem(pem_text):
    return pins_of_der(ssl.PEM_cert_to_DER_cert(pem_text))


def matches(pins, der):
    """True when the DER certificate `der` is the one `pins` names: a pin (or a list of pins, empty ones ignored) that
    is its whole-certificate or its public-key SHA-256."""
    wanted = {pins} if isinstance(pins, str) else set(pins or ())
    wanted.discard("")
    if not wanted or not der:
        return False
    try:
        return bool(wanted & set(pins_of_der(der)))
    except ValueError:
        return hashlib.sha256(der).hexdigest() in wanted


def _create(cert_path, key_path, hosts, key=None):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
