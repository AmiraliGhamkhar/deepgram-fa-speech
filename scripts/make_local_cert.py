#!/usr/bin/env python3
"""Generate a self-signed TLS cert for local host development.

Writes %USERPROFILE%\\.medical-stt\\secure\\localhost-cert.pem and
``localhost-key.pem`` (SAN: localhost + 127.0.0.1), outside the repo so
``scripts/scan_secrets.py`` stays clean.

Run:  python scripts/make_local_cert.py
Needs: pip install cryptography
"""
from __future__ import annotations

import datetime
import ipaddress
import sys
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError as exc:
    print("cryptography is required: pip install cryptography", file=sys.stderr)
    raise SystemExit(2) from exc


def _secure_dir() -> Path:
    if sys.platform == "win32":
        base = Path.home() / ".medical-stt" / "secure"
    else:
        base = Path.home() / ".medical-stt" / "secure"
    base.mkdir(parents=True, exist_ok=True)
    return base


def main() -> int:
    secure = _secure_dir()
    cert_path = secure / "localhost-cert.pem"
    key_path = secure / "localhost-key.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        import os

        os.chmod(key_path, 0o600)
    except OSError:
        pass
    print(f"wrote {cert_path}")
    print(f"wrote {key_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
