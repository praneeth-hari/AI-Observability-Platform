"""
Create a private certificate authority (CA) and a server certificate for the collector.

  python scripts/make_certs.py                      # writes ./certs/ (localhost + docker names)
  python scripts/make_certs.py --host otel.mycorp   # add more names the collector is reached by

Files:  certs/ca.crt      public CA certificate - agents trust this (safe to share)
        certs/server.crt  collector certificate
        certs/server.key  collector private key  (SECRET - never commit; certs/ is git-ignored)
        certs/ca.key      CA private key         (SECRET - keep offline in real deployments)

For DEVELOPMENT. In production use your company CA / cert-manager (planned in Step 8).
"""
import argparse
import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _write(path: Path, data: bytes, mode: int):
    path.write_bytes(data)
    os.chmod(path, mode)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AI Observability (dev)"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_certs(outdir, hosts=("localhost", "otel-collector", "127.0.0.1", "::1"), server_days=397):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("AI Observability Dev CA")).issuer_name(_name("AI Observability Dev CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))

    sans = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv = (x509.CertificateBuilder().subject_name(_name(hosts[0])).issuer_name(ca.subject)
           .public_key(srv_key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now).not_valid_after(now + dt.timedelta(days=server_days))
           .add_extension(x509.SubjectAlternativeName(sans), critical=False)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .add_extension(x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .sign(ca_key, hashes.SHA256()))

    pem = serialization.Encoding.PEM
    key_fmt = dict(encoding=pem, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption())
    _write(out / "ca.crt", ca.public_bytes(pem), 0o644)
    _write(out / "ca.key", ca_key.private_bytes(**key_fmt), 0o600)
    _write(out / "server.crt", srv.public_bytes(pem), 0o644)
    # readable by the container's non-root user; DEV ONLY (production: mounted secret, see Step 8)
    _write(out / "server.key", srv_key.private_bytes(**key_fmt), 0o644)
    try:
        os.chmod(out, 0o755)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "certs"))
    ap.add_argument("--host", action="append", default=[], help="extra DNS name or IP for the server certificate")
    a = ap.parse_args()
    hosts = ["localhost", "otel-collector", "127.0.0.1", "::1"] + a.host
    path = make_certs(a.out, hosts)
    print(f"Created certificates in {path}\n  trusted by agents: ca.crt\n  valid for: {', '.join(hosts)}")
