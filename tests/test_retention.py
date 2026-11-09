"""Retention tests: the TTL rules must be valid, safe, and really delete old rows."""
import tempfile

import pytest

import retention
from retention import TABLES, ttl_statements


def test_one_statement_per_table_with_correct_time_column():
    stmts = ttl_statements(30)
    assert len(stmts) == len(TABLES)
    for table, column in TABLES.items():
        assert f"ALTER TABLE default.{table} MODIFY TTL toDateTime({column}) + INTERVAL 30 DAY" in stmts


@pytest.mark.parametrize("bad", [0, -5, 3651, 99999, 2.5, "30", None, True])
def test_invalid_days_are_rejected(bad):
    with pytest.raises(ValueError):
        ttl_statements(bad)


def test_missing_tables_are_skipped():
    stmts = ttl_statements(30, existing={"traces", "scores"})
    assert len(stmts) == 2 and all("traces" in s or "scores" in s for s in stmts)


def test_audit_logs_untouched_unless_requested():
    assert not any("audit_logs" in s for s in ttl_statements(30))
    with_audit = ttl_statements(30, audit_days=365)
    assert any("audit_logs" in s and "INTERVAL 365 DAY" in s for s in with_audit)


def test_apply_sends_sql_on_stdin_and_never_puts_secrets_on_the_command_line():
    calls = []

    def fake(cmd, stdin=None):
        calls.append((cmd, stdin))
        if stdin and stdin.startswith("SHOW TABLES"):
            return "traces\nobservations\nscores\n"
        return ""
    retention.apply(14, runner=fake)
    sqls = [c for c in calls if c[1] and "MODIFY TTL" in c[1]]
    assert len(sqls) == 1 and sqls[0][1].count("MODIFY TTL") == 3 and "INTERVAL 14 DAY" in sqls[0][1]
    flat = " ".join(" ".join(c[0]) for c in calls)
    assert '"$CLICKHOUSE_PASSWORD"' in flat and "--password clickhouse" not in flat   # read inside the container
    assert any("mc ilm rule add --expire-days 14" in " ".join(c[0]) for c in calls)  # storage expiry too


def test_minio_failure_is_not_fatal(capsys):
    def fake(cmd, stdin=None):
        if "minio" in cmd:
            raise RuntimeError("mc: unknown command")
        return "traces\n" if (stdin or "").startswith("SHOW") else ""
    retention.apply(30, runner=fake)
    assert "WARNING" in capsys.readouterr().out


def test_ttl_really_deletes_old_rows_in_a_real_clickhouse():
    """Run the generated SQL against an embedded ClickHouse engine (chdb) on Langfuse-shaped tables."""
    chdb = pytest.importorskip("chdb", reason="pip install chdb to run this test against a real ClickHouse")
    from chdb import session
    s = session.Session(tempfile.mkdtemp())
    s.query("CREATE DATABASE t")
    for table, col in TABLES.items():
        s.query(f"""CREATE TABLE t.{table} (id String, {col} DateTime64(3), event_ts DateTime64(3), is_deleted UInt8)
                    ENGINE = ReplacingMergeTree(event_ts, is_deleted) PARTITION BY toYYYYMM({col}) ORDER BY id""")
        s.query(f"INSERT INTO t.{table} VALUES ('fresh', now(), now(), 0), ('recent', now() - INTERVAL 5 DAY, now(), 0), "
                f"('stale', now() - INTERVAL 45 DAY, now(), 0), ('ancient', now() - INTERVAL 400 DAY, now(), 0)")
    for stmt in ttl_statements(30, database="t"):
        s.query(stmt)
    for table in TABLES:
        s.query(f"OPTIMIZE TABLE t.{table} FINAL")
        rows = str(s.query(f"SELECT id FROM t.{table} ORDER BY id", "CSV")).split()
        assert rows == ['"fresh"', '"recent"'], f"{table}: kept {rows}"


def test_time_columns_match_langfuse_schema():
    """Pinned independently of the tool, from Langfuse's ClickHouse migrations (a typo here silently breaks retention)."""
    assert TABLES == {
        "traces": "timestamp",
        "observations": "start_time",
        "scores": "timestamp",
        "events_full": "start_time",
        "events_core": "start_time",
        "blob_storage_file_log": "created_at",
    }
    assert retention.AUDIT_TABLE == ("audit_logs", "timestamp")
