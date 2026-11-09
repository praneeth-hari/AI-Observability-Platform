"""Secrets hygiene tests (no collector needed, run in seconds)."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from secrets_tool import SECRETS, generate, parse_env, problems

ROOT = Path(__file__).resolve().parent.parent


def test_placeholder_template_is_flagged_as_weak():
    """.env.example is only a template: every secret in it must be reported as weak."""
    bad = problems(parse_env(ROOT / ".env.example"))
    assert set(bad) == set(SECRETS), f"template secrets not flagged: {set(SECRETS) - set(bad)}"


def test_generated_secrets_are_strong_unique_and_well_formed(tmp_path):
    out = tmp_path / "generated.env"
    generate(ROOT / ".env.example", out)
    values = parse_env(out)
    assert problems(values) == {}
    assert re.fullmatch(r"[0-9a-f]{64}", values["ENCRYPTION_KEY"])
    assert values["AGENT_API_KEY"].startswith("aobs_")
    assert len({values[k] for k in SECRETS}) == len(SECRETS), "secrets must all be different"


def test_two_generations_differ(tmp_path):
    generate(ROOT / ".env.example", tmp_path / "a.env")
    generate(ROOT / ".env.example", tmp_path / "b.env")
    a, b = parse_env(tmp_path / "a.env"), parse_env(tmp_path / "b.env")
    assert all(a[k] != b[k] for k in SECRETS)


@pytest.mark.parametrize("value,expected_reason", [
    ("", "missing"), ("postgres", "default"), ("change-me-please-long-value-xxxxxxxxx", "placeholder"),
    ("short", "too short")])
def test_weak_values_are_detected(value, expected_reason):
    assert expected_reason in problems({"POSTGRES_PASSWORD": value})["POSTGRES_PASSWORD"]


def test_reused_secret_is_detected():
    same = "a" * 8 + "1b2c3d4e5f60718293a4b5c6"
    bad = problems({"POSTGRES_PASSWORD": same, "CLICKHOUSE_PASSWORD": same})
    assert "same value" in bad["CLICKHOUSE_PASSWORD"]


def test_compose_has_no_weak_secret_defaults():
    """Variables like ${POSTGRES_PASSWORD:-postgres} silently run the stack with a known password."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    secret_vars = "|".join(SECRETS)
    bad = re.findall(r"\$\{(?:%s):-[^}]*\}" % secret_vars, text)
    assert not bad, f"secrets with fallback defaults: {bad}"


@pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")
def test_compose_accepts_generated_env_and_rejects_missing(tmp_path):
    good = tmp_path / "good.env"
    generate(ROOT / ".env.example", good)
    ok = subprocess.run(["docker", "compose", "--env-file", str(good), "config", "-q"], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in SECRETS}
    bad = subprocess.run(["docker", "compose", "--env-file", str(empty), "config", "-q"], cwd=ROOT,
                         capture_output=True, text=True, env=env)
    assert bad.returncode != 0, "compose must refuse to start without secrets"


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        return [ROOT / f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "certs", "__pycache__", ".pytest_cache", "node_modules"}
        files = []
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [d for d in dn if d not in skip]
            files += [Path(dp) / f for f in fn if f not in (".env", ".env.generated")]
        return files


SECRET_PATTERNS = {
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "Hugging Face token": r"\bhf_[A-Za-z0-9]{30,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "OpenAI-style key": r"\bsk-[A-Za-z0-9]{32,}",
    "agent API key": r"\baobs_[0-9a-f]{40,}",
    "Langfuse secret key": r"\bsk-lf-[0-9a-f]{32,}",
}


def test_no_real_secrets_in_tracked_files():
    leaks = []
    for f in _tracked_files():
        if not f.is_file() or f.suffix in {".png", ".jpg", ".zip", ".gif"}:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for label, pat in SECRET_PATTERNS.items():
            if re.search(pat, text):
                leaks.append(f"{f.relative_to(ROOT)}: {label}")
    assert not leaks, "possible secrets committed:\n" + "\n".join(leaks)


def test_gitignore_protects_secret_files():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    for must in (".env", "certs/", ".venv/"):
        assert must in ignored, f"{must} must be git-ignored"
