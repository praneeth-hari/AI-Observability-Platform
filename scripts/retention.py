"""
Data retention for the AI observability stack: automatically delete old telemetry.

Langfuse's own retention setting is a paid feature (Cloud Pro / self-hosted Enterprise), so this
tool applies open-source ClickHouse TTL rules instead, plus an expiry rule on the MinIO bucket.

  python scripts/retention.py plan  --days 30      # show the SQL, change nothing (no Docker needed)
  python scripts/retention.py apply --days 30      # apply to the running stack
  python scripts/retention.py show                 # show the TTL currently set on each table

Default days: RETENTION_DAYS in .env, else 30.  Optional: --audit-days N also expires audit_logs
(left untouched by default, because audit trails are usually kept longer).

How it behaves: ClickHouse removes expired rows in the background when it merges data parts
(normally within hours, not instantly). Re-run `apply` after upgrading Langfuse to be sure the rules are in place.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# table -> time column that decides a row's age (taken from Langfuse's ClickHouse migrations)
TABLES = {
    "traces": "timestamp",
    "observations": "start_time",
    "scores": "timestamp",
    "events_full": "start_time",
    "events_core": "start_time",
    "blob_storage_file_log": "created_at",
}
AUDIT_TABLE = ("audit_logs", "timestamp")
DEFAULT_DAYS = 30
MINIO_PREFIXES = ("events/", "media/")


def validate_days(days):
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 3650):
        raise ValueError(f"days must be a whole number between 1 and 3650, got {days!r}")
    return days


def ttl_statements(days, existing=None, database="default", audit_days=None):
    """ALTER statements. `existing` limits them to tables that really exist (None = all)."""
    validate_days(days)
    plan = [(t, c, days) for t, c in TABLES.items()]
    if audit_days is not None:
        plan.append((*AUDIT_TABLE, validate_days(audit_days)))
    return [f"ALTER TABLE {database}.{t} MODIFY TTL toDateTime({c}) + INTERVAL {d} DAY"
            for t, c, d in plan if existing is None or t in existing]


def read_env_days(env_file=ROOT / ".env"):
    try:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*RETENTION_DAYS\s*=\s*(\d+)", line)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return DEFAULT_DAYS


# ---------------- running things inside the containers ----------------
def run(cmd, stdin=None):
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd[:5])} ...\n{p.stderr.strip()}")
    return p.stdout


CH = ["docker", "compose", "exec", "-T", "clickhouse", "sh", "-c",
      'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery']


def ch(sql, runner=run):
    """Run SQL in the ClickHouse container (credentials come from the container's own environment)."""
    return runner(CH, sql)


def existing_tables(runner=run, database="default"):
    out = ch(f"SHOW TABLES FROM {database}", runner)
    return {line.strip() for line in out.splitlines() if line.strip()}


def minio_commands(days):
    validate_days(days)
    rules = " && ".join(f"mc ilm rule add --expire-days {days} --prefix {p} local/langfuse" for p in MINIO_PREFIXES)
    script = ('mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null '
              f'&& (mc ilm rule rm --all --force local/langfuse >/dev/null 2>&1 || true) && {rules}')
    return ["docker", "compose", "exec", "-T", "minio", "sh", "-c", script]


def apply(days, audit_days=None, skip_storage=False, runner=run):
    tables = existing_tables(runner)
    stmts = ttl_statements(days, tables, audit_days=audit_days)
    missing = [t for t in TABLES if t not in tables]
    ch(";\n".join(stmts) + ";", runner)
    print(f"ClickHouse: TTL of {days} days set on {len(stmts)} table(s).")
    if missing:
        print(f"  (not present in this Langfuse version, skipped: {', '.join(missing)})")
    if not skip_storage:
        try:
            runner(minio_commands(days))
            print(f"MinIO: objects under {', '.join(MINIO_PREFIXES)} expire after {days} days.")
        except RuntimeError as e:
            print("WARNING: could not set the MinIO expiry rule (ClickHouse rules are in place).\n" + str(e))
    return stmts


def show(runner=run):
    names = ",".join(f"'{t}'" for t in [*TABLES, AUDIT_TABLE[0]])
    sql = ("SELECT name, if(position(create_table_query, 'TTL ') > 0, "
           "extract(create_table_query, 'TTL [^\\n]+'), '(no TTL - kept forever)') "
           f"FROM system.tables WHERE database = 'default' AND name IN ({names}) ORDER BY name FORMAT TSV")
    return ch(sql, runner)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=None)
        p.add_argument("--audit-days", type=int, default=None)
        if name == "apply":
            p.add_argument("--skip-storage", action="store_true", help="do not touch MinIO")
    sub.add_parser("show")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "show":
            print(show())
            return 0
        days = a.days if a.days is not None else read_env_days()
        if a.cmd == "plan":
            print("\n".join(s + ";" for s in ttl_statements(days, audit_days=a.audit_days)))
            return 0
        apply(days, a.audit_days, a.skip_storage)
        return 0
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
