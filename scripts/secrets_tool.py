"""
Check and generate the project's secrets (.env).

  python scripts/secrets_tool.py check              # audit .env: weak, default, short or reused secrets?
  python scripts/secrets_tool.py check --file .env.example
  python scripts/secrets_tool.py generate           # writes .env.generated with strong random secrets

`generate` never overwrites .env. Review the new file, rename it to .env, then reset the data:
    docker compose down -v      (database/ClickHouse/MinIO passwords are baked into their volumes)
    docker compose up -d
Exit code of `check` is 1 when something is weak, so it can gate a deployment.
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> (kind, minimum length)
SECRETS = {
    "NEXTAUTH_SECRET": ("hex", 32),
    "SALT": ("hex", 32),
    "ENCRYPTION_KEY": ("hex64", 64),
    "POSTGRES_PASSWORD": ("hex", 24),
    "CLICKHOUSE_PASSWORD": ("hex", 24),
    "REDIS_AUTH": ("hex", 24),
    "MINIO_ROOT_PASSWORD": ("hex", 24),
    "LANGFUSE_INIT_PROJECT_SECRET_KEY": ("sk-lf-", 32),
    "LANGFUSE_INIT_USER_PASSWORD": ("password", 16),
    "AGENT_API_KEY": ("aobs_", 40),
}

KNOWN_WEAK = {"postgres", "clickhouse", "myredissecret", "miniosecret", "mysecret", "mysalt", "changeme123!",
              "password", "secret", "admin", "123456", "sk-lf-local-prototype"}


def parse_env(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return values


def problems(values):
    """Return {name: reason} for every weak secret."""
    bad, seen = {}, {}
    for name, (_, minlen) in SECRETS.items():
        v = values.get(name, "")
        low = v.lower()
        if not v:
            bad[name] = "missing"
        elif "change-me" in low or "changeme" in low or "change_me" in low:
            bad[name] = "still a placeholder"
        elif low in KNOWN_WEAK:
            bad[name] = "well-known default value"
        elif len(v) < minlen:
            bad[name] = f"too short ({len(v)} < {minlen})"
        elif name == "ENCRYPTION_KEY" and (not re.fullmatch(r"[0-9a-fA-F]{64}", v) or len(set(v)) < 8):
            bad[name] = "must be 64 random hex characters"
        elif v in seen:
            bad[name] = f"same value as {seen[v]} (every secret must be different)"
        seen.setdefault(v, name)
    return bad


def generate_value(kind):
    if kind == "hex64":
        return secrets.token_hex(32)
    if kind == "hex":
        return secrets.token_hex(24)
    if kind == "sk-lf-":
        return "sk-lf-" + secrets.token_hex(24)
    if kind == "aobs_":
        return "aobs_" + secrets.token_hex(24)
    if kind == "password":  # letters+digits, plus symbols to satisfy password policies; no URL-unsafe characters
        return secrets.token_urlsafe(18) + "!9"
    raise ValueError(kind)


def generate(template_path, out_path):
    text = Path(template_path).read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) in SECRETS:
            comment = ""
            if " #" in m.group(2):
                comment = "  #" + m.group(2).split(" #", 1)[1].replace("CHANGEME", "generated")
            line = f"{m.group(1)}={generate_value(SECRETS[m.group(1)][0])}{comment}"
        lines.append(line)
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--file", default=str(ROOT / ".env"))
    g = sub.add_parser("generate")
    g.add_argument("--template", default=str(ROOT / ".env.example"))
    g.add_argument("--out", default=str(ROOT / ".env.generated"))
    a = ap.parse_args()

    if a.cmd == "check":
        if not Path(a.file).exists():
            print(f"{a.file} not found"); return 2
        bad = problems(parse_env(a.file))
        for name in SECRETS:
            print(f"  {'WEAK' if name in bad else 'ok  '}  {name}" + (f"  -> {bad[name]}" if name in bad else ""))
        print("\nAll secrets look strong." if not bad else f"\n{len(bad)} weak secret(s). Run: python scripts/secrets_tool.py generate")
        return 1 if bad else 0

    if Path(a.out).exists():
        print(f"{a.out} already exists - delete it first."); return 2
    generate(a.template, a.out)
    print(f"Wrote {a.out}\nNext: review it, rename to .env, then 'docker compose down -v' and 'docker compose up -d'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
