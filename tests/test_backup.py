"""Backup tests: sequence of actions, integrity checks, and safety rules (Docker is simulated)."""
import datetime as dt
import json
import tarfile
from pathlib import Path

import pytest

import backup

NOW = dt.datetime(2026, 11, 9, 21, 45, 0)
CONFIG_JSON = json.dumps({"volumes": {k: {"name": f"proj_{k}"} for k in
                                      [*backup.VOLUMES, "redis_data"]}})


class FakeDocker:
    """Records every command; emulates the `tar czf` / `tar xzf` volume jobs with real files."""
    def __init__(self, fail_on=None):
        self.calls, self.fail_on, self.restored = [], fail_on, []

    def __call__(self, cmd, stdin=None):
        self.calls.append(cmd)
        flat = " ".join(cmd)
        if cmd[:3] == ["docker", "compose", "config"]:
            return CONFIG_JSON
        if self.fail_on and self.fail_on in flat:
            raise RuntimeError("simulated docker failure")
        if cmd[:2] == ["docker", "run"] and "tar" in cmd and "czf" in cmd:
            bind = next(a for a in cmd if "target=/backup" in a)
            dest = Path(bind.split("source=")[1].split(",target")[0])
            name = cmd[cmd.index("czf") + 1].split("/")[-1]
            src = dest / "_payload.txt"
            src.write_text(f"data of {name}")
            with tarfile.open(dest / name, "w:gz") as t:
                t.add(src, arcname="payload.txt")
            src.unlink()
        if cmd[:2] == ["docker", "run"] and "xzf" in flat:
            self.restored.append(next(a for a in cmd if a.startswith("type=volume")).split("source=")[1].split(",")[0])
        return ""

    def verbs(self):
        return [" ".join(c[:3]) for c in self.calls if c[:2] == ["docker", "compose"] and c[2] != "config"]


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("\n".join(f"{k}=value-{k}-0123456789abcdef" for k in backup.FINGERPRINT_KEYS) + "\n")
    return p


def make(tmp_path, env_file, docker=None):
    docker = docker or FakeDocker()
    return backup.create(tmp_path / "backups", docker, NOW, env_file), docker


def test_create_stops_stack_archives_three_volumes_then_restarts(tmp_path, env_file):
    dest, docker = make(tmp_path, env_file)
    assert dest.name == "backup-20261109-214500"
    assert docker.verbs() == ["docker compose stop", "docker compose up"]
    runs = [c for c in docker.calls if c[:2] == ["docker", "run"]]
    assert len(runs) == 3
    assert [r[r.index("czf") + 1] for r in runs] == [f"/backup/{k}.tar.gz" for k in backup.VOLUMES]
    assert all("readonly" in " ".join(r) for r in runs)              # source volume mounted read-only
    assert docker.calls.index(["docker", "compose", "stop"]) < docker.calls.index(runs[0])
    assert backup.verify(dest) == []


def test_manifest_has_checksums_and_secret_fingerprint_but_no_secrets(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    text = (dest / "manifest.json").read_text()
    m = json.loads(text)
    assert set(m["volumes"]) == set(backup.VOLUMES)
    assert all(len(v["sha256"]) == 64 and v["bytes"] > 0 for v in m["volumes"].values())
    assert m["secrets_fingerprint"] and len(m["secrets_fingerprint"]) == 64
    assert "value-POSTGRES_PASSWORD" not in text and "value-SALT" not in text


def test_stack_is_restarted_and_partial_backup_removed_when_archiving_fails(tmp_path, env_file):
    docker = FakeDocker(fail_on="clickhouse_data")
    with pytest.raises(RuntimeError):
        backup.create(tmp_path / "backups", docker, NOW, env_file)
    assert docker.verbs()[-1] == "docker compose up"                   # services come back even on failure
    assert backup.list_backups(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob("backup-*"))


def test_verify_detects_corruption_missing_files_and_bad_manifest(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    (dest / "minio_data.tar.gz").write_bytes(b"corrupted!")
    assert any("minio_data" in p for p in backup.verify(dest))
    (dest / "postgres_data.tar.gz").unlink()
    assert any("postgres_data" in p and "missing" in p for p in backup.verify(dest))
    (dest / "manifest.json").write_text("{not json")
    assert backup.verify(dest) == ["manifest.json missing or unreadable"]


def test_restore_requires_explicit_confirmation(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    docker = FakeDocker()
    with pytest.raises(RuntimeError, match="--yes"):
        backup.restore(dest, yes=False, runner=docker, env_file=env_file)
    assert docker.calls == []                                           # nothing touched


def test_restore_refuses_a_corrupted_backup_without_changing_anything(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    (dest / "clickhouse_data.tar.gz").write_bytes(b"junk")
    docker = FakeDocker()
    with pytest.raises(RuntimeError, match="not intact"):
        backup.restore(dest, yes=True, runner=docker, env_file=env_file)
    assert docker.calls == []


def test_restore_refuses_when_env_secrets_differ(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    other = tmp_path / "other.env"
    other.write_text(env_file.read_text().replace("value-SALT", "different-SALT"))
    docker = FakeDocker()
    with pytest.raises(RuntimeError, match="secrets"):
        backup.restore(dest, yes=True, runner=docker, env_file=other)
    assert docker.calls == []


def test_restore_replaces_each_volume_between_down_and_up(tmp_path, env_file):
    dest, _ = make(tmp_path, env_file)
    docker = FakeDocker()
    backup.restore(dest, yes=True, runner=docker, env_file=env_file)
    assert docker.verbs() == ["docker compose down", "docker compose up"]
    assert docker.restored == [f"proj_{k}" for k in backup.VOLUMES]
    assert "-v" not in docker.calls[docker.calls.index(["docker", "compose", "down"])]   # never `down -v`


def test_prune_keeps_only_newest(tmp_path, env_file):
    for minute in range(5):
        backup.create(tmp_path / "backups", FakeDocker(), NOW + dt.timedelta(minutes=minute), env_file)
    removed = backup.prune(2, tmp_path / "backups")
    assert len(removed) == 3
    left = [p.name for p, _ in backup.list_backups(tmp_path / "backups")]
    assert left == ["backup-20261109-214800", "backup-20261109-214900"]
    with pytest.raises(ValueError):
        backup.prune(0, tmp_path / "backups")
