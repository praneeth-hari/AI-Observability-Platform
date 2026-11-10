"""Tests for scripts/k8s_secrets.py - it must never leak the CA key and must refuse weak secrets."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import k8s_secrets  # noqa: E402
import secrets_tool  # noqa: E402
from make_certs import make_certs  # noqa: E402


@pytest.fixture
def good(tmp_path):
    env = tmp_path / ".env"
    template = ROOT / ".env.example"
    secrets_tool.generate(template, env)
    certs = make_certs(tmp_path / "certs")
    return env, certs


def test_builds_both_secrets_with_all_keys(good):
    env, certs = good
    out = k8s_secrets.build(env, certs)
    names = {i["metadata"]["name"]: i for i in out["items"]}
    assert set(names) == {"ai-obs-secrets", "ai-obs-collector-tls"}
    assert set(names["ai-obs-secrets"]["stringData"]) == set(secrets_tool.SECRETS)
    assert set(names["ai-obs-collector-tls"]["stringData"]) == {"server.crt", "server.key", "ca.crt"}


def test_ca_private_key_is_never_included(good):
    env, certs = good
    assert (Path(certs) / "ca.key").exists(), "test precondition: make_certs creates ca.key"
    ca_key = (Path(certs) / "ca.key").read_text()
    assert ca_key.strip().splitlines()[1] not in json.dumps(k8s_secrets.build(env, certs))


def test_refuses_weak_secrets(good, tmp_path):
    env, certs = good
    shutil_env = tmp_path / "weak.env"
    shutil_env.write_text(Path(env).read_text().replace("AGENT_API_KEY=", "AGENT_API_KEY=change-me-", 1))
    with pytest.raises(SystemExit, match="weak"):
        k8s_secrets.build(shutil_env, certs)


def test_refuses_the_example_file(good):
    _, certs = good
    with pytest.raises(SystemExit):
        k8s_secrets.build(ROOT / ".env.example", certs)


def test_missing_certs_is_a_clear_error(good, tmp_path):
    env, _ = good
    with pytest.raises(SystemExit, match="make_certs"):
        k8s_secrets.build(env, tmp_path / "nope")


def test_namespace_is_applied(good):
    env, certs = good
    out = k8s_secrets.build(env, certs, namespace="ai-obs")
    assert all(i["metadata"]["namespace"] == "ai-obs" for i in out["items"])
