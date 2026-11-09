"""
Backup and restore of the stack's data (Postgres, ClickHouse, MinIO).

  python scripts/backup.py create            # cold backup into backups/backup-<timestamp>/
  python scripts/backup.py list
  python scripts/backup.py verify backups/backup-20261109-214500
  python scripts/backup.py restore backups/backup-20261109-214500 --yes
  python scripts/backup.py prune --keep 7    # keep only the 7 newest backups

How it works: the stack is stopped for about a minute (agents' traces sent during that time are lost),
each data volume is archived to a .tar.gz, every file gets a SHA-256 checksum in manifest.json, then the stack
is started again. A cold backup is slower than a live dump but guaranteed consistent.

IMPORTANT
- Backups do NOT contain your .env (secrets). Keep .env in a password manager / secret store. The database
  passwords and ENCRYPTION_KEY are baked into the data: restoring with a different .env breaks the stack.
  The manifest stores a one-way fingerprint of those secrets, and `restore` refuses if they differ.
- Backups hold (already redacted) trace data. Store them somewhere access-controlled, off this machine.
- A backup you have never restored is only a hope: run a restore drill (see README).
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "backups"
VOLUMES = ["postgres_data", "clickhouse_data", "minio_data"]   # redis only holds a disposable queue
IMAGE = "alpine:3.20"
FINGERPRINT_KEYS = ["POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "MINIO_ROOT_PASSWORD", "ENCRYPTION_KEY", "SALT"]
NAME_RE = re.compile(r"^backup-\d{8}-\d{6}$")


def run(cmd, stdin=None):
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd[:6])} ...\n{p.stderr.strip()}")
    return p.stdout


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def secrets_fingerprint(env_file=ROOT / ".env"):
    """One-way hash of the secrets baked into the data. Safe to store; lets restore detect a wrong .env."""
    values = {}
    try:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.split(" #", 1)[0].strip()
    except OSError:
        return None
    if not all(values.get(k) for k in FINGERPRINT_KEYS):
        return None
    return hashlib.sha256("\n".join(values[k] for k in FINGERPRINT_KEYS).encode()).hexdigest()


def resolve_volumes(runner):
    """Real Docker volume names (they carry the compose project prefix)."""
    data = json.loads(runner(["docker", "compose", "config", "--format", "json"]))
    return {key: data["volumes"][key]["name"] for key in VOLUMES}


def _mounts(volume, backup_dir, readonly_volume):
    vol = f"type=volume,source={volume},target=/data" + (",readonly" if readonly_volume else "")
    return ["--mount", vol, "--mount", f"type=bind,source={backup_dir},target=/backup" + ("" if readonly_volume else ",readonly")]


def create(backups_dir=BACKUPS, runner=run, now=None, env_file=ROOT / ".env"):
    now = now or dt.datetime.now()
    volumes = resolve_volumes(runner)
    dest = (Path(backups_dir) / f"backup-{now:%Y%m%d-%H%M%S}").resolve()
    dest.mkdir(parents=True)
    files = {}
    runner(["docker", "compose", "stop"])
    try:
        for key, volume in volumes.items():
            name = f"{key}.tar.gz"
            runner(["docker", "run", "--rm", *_mounts(volume, dest, True), IMAGE,
                    "tar", "czf", f"/backup/{name}", "-C", "/data", "."])
            files[key] = {"file": name, "volume": volume}
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)   # never leave a half-finished backup that looks real
        raise
    finally:
        runner(["docker", "compose", "up", "-d"])
    for info in files.values():
        p = dest / info["file"]
        info["bytes"], info["sha256"] = p.stat().st_size, sha256(p)
    manifest = {"created_at": now.isoformat(timespec="seconds"), "tool_version": 1, "image": IMAGE,
                "secrets_fingerprint": secrets_fingerprint(env_file), "volumes": files}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def verify(backup_dir):
    """Return a list of problems (empty = backup is intact)."""
    d = Path(backup_dir)
    try:
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["manifest.json missing or unreadable"]
    problems = []
    for key in VOLUMES:
        info = manifest.get("volumes", {}).get(key)
        if not info:
            problems.append(f"{key}: not listed in manifest")
            continue
        p = d / info["file"]
        if not p.exists():
            problems.append(f"{info['file']}: file missing")
        elif p.stat().st_size != info["bytes"]:
            problems.append(f"{info['file']}: size differs from manifest")
        elif sha256(p) != info["sha256"]:
            problems.append(f"{info['file']}: checksum mismatch (file was changed or corrupted)")
        else:
            try:
                with tarfile.open(p, "r:gz") as t:
                    for _ in t:
                        pass
            except (tarfile.TarError, OSError, EOFError) as e:
                problems.append(f"{info['file']}: archive unreadable ({e})")
    return problems


def restore(backup_dir, yes=False, runner=run, env_file=ROOT / ".env"):
    if not yes:
        raise RuntimeError("restore REPLACES all current data. Re-run with --yes to confirm.")
    problems = verify(backup_dir)
    if problems:
        raise RuntimeError("backup is not intact, nothing was changed:\n  " + "\n  ".join(problems))
    d = Path(backup_dir).resolve()
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    saved, current = manifest.get("secrets_fingerprint"), secrets_fingerprint(env_file)
    if saved and saved != current:
        raise RuntimeError("the secrets in .env differ from those used when this backup was made "
                           "(database passwords / ENCRYPTION_KEY). Restore the original .env first; nothing was changed.")
    volumes = resolve_volumes(runner)
    runner(["docker", "compose", "down"])      # keeps volumes
    for key, volume in volumes.items():
        name = manifest["volumes"][key]["file"]
        runner(["docker", "run", "--rm", *_mounts(volume, d, False), IMAGE, "sh", "-c",
                f"find /data -mindepth 1 -delete && tar xzf /backup/{name} -C /data"])
    runner(["docker", "compose", "up", "-d"])


def list_backups(backups_dir=BACKUPS):
    out = []
    for p in sorted(Path(backups_dir).glob("backup-*")) if Path(backups_dir).exists() else []:
        if p.is_dir() and NAME_RE.match(p.name) and (p / "manifest.json").exists():
            size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
            out.append((p, size))
    return out


def prune(keep, backups_dir=BACKUPS):
    if keep < 1:
        raise ValueError("--keep must be at least 1")
    found = list_backups(backups_dir)
    doomed = [p for p, _ in found[:-keep]] if len(found) > keep else []
    for p in doomed:
        shutil.rmtree(p)
    return doomed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create")
    sub.add_parser("list")
    v = sub.add_parser("verify"); v.add_argument("dir")
    r = sub.add_parser("restore"); r.add_argument("dir"); r.add_argument("--yes", action="store_true")
    p = sub.add_parser("prune"); p.add_argument("--keep", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "create":
            dest = create()
            problems = verify(dest)
            print(f"Backup written to {dest}\n" + ("Verified OK." if not problems else "PROBLEMS: " + "; ".join(problems)))
            return 1 if problems else 0
        if a.cmd == "list":
            for p_, size in list_backups():
                print(f"{p_.name}  {size / 1e6:.1f} MB")
            return 0
        if a.cmd == "verify":
            problems = verify(a.dir)
            print("Backup is intact." if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
            return 1 if problems else 0
        if a.cmd == "restore":
            restore(a.dir, a.yes)
            print("Restored. Give the services a minute to start, then check Langfuse.")
            return 0
        gone = prune(a.keep)
        print(f"Removed {len(gone)} old backup(s).")
        return 0
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
