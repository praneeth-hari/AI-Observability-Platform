"""
Create the two Kubernetes Secrets the Helm chart needs, from your local .env and certs/ folder.

  python scripts/k8s_secrets.py | kubectl apply -n ai-obs -f -

  ai-obs-secrets          every password/key from .env (the ten secrets in secrets_tool.py)
  ai-obs-collector-tls    certs/server.crt + certs/server.key (+ ca.crt for convenience)

Safety: it prints the manifest to the screen/pipe ONLY (never writes a file), refuses weak or
placeholder secrets (same rules as `secrets_tool.py check`), and never includes the CA private key.
Do not paste its output anywhere. In production a secrets vault replaces this script (Step 8).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_tool import SECRETS, parse_env, problems  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TLS_FILES = ("server.crt", "server.key", "ca.crt")   # ca.key is deliberately NOT here


def build(env_file, cert_dir, name="ai-obs-secrets", tls_name="ai-obs-collector-tls", namespace=None):
    values = parse_env(env_file)
    bad = problems(values)
    if bad:
        raise SystemExit("Refusing: weak secrets in %s: %s\nRun: python scripts/secrets_tool.py generate"
                         % (env_file, ", ".join(f"{k} ({v})" for k, v in bad.items())))
    cert_dir = Path(cert_dir)
    missing = [f for f in TLS_FILES if not (cert_dir / f).is_file()]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in {cert_dir}. Run: python scripts/make_certs.py")

    def secret(secret_name, data):
        meta = {"name": secret_name}
        if namespace:
            meta["namespace"] = namespace
        return {"apiVersion": "v1", "kind": "Secret", "metadata": meta, "type": "Opaque", "stringData": data}

    return {"apiVersion": "v1", "kind": "List", "items": [
        secret(name, {k: values[k] for k in SECRETS}),
        secret(tls_name, {f: (cert_dir / f).read_text(encoding="utf-8") for f in TLS_FILES}),
    ]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--certs", default=str(ROOT / "certs"))
    ap.add_argument("--name", default="ai-obs-secrets")
    ap.add_argument("--tls-name", default="ai-obs-collector-tls")
    ap.add_argument("--namespace", default=None)
    a = ap.parse_args()
    print(json.dumps(build(a.env, a.certs, a.name, a.tls_name, a.namespace), indent=2))


if __name__ == "__main__":
    main()
