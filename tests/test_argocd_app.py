"""The Argo CD Application must point at things that really exist in this repo."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APP = yaml.safe_load((ROOT / "deploy" / "argocd" / "application.yaml").read_text())
SPEC = APP["spec"]


def test_kind_and_namespace():
    assert APP["kind"] == "Application" and APP["metadata"]["namespace"] == "argocd"


def test_points_at_the_real_chart_and_values_file():
    chart = ROOT / SPEC["source"]["path"]
    assert (chart / "Chart.yaml").is_file()
    for f in SPEC["source"]["helm"]["valueFiles"]:
        assert (chart / f).is_file(), f"{f} is not in the chart folder"


def test_release_name_matches_what_the_docs_and_tests_assume():
    assert SPEC["source"]["helm"]["releaseName"] == "ai-observability"   # service names depend on it


def test_deploys_to_the_ai_obs_namespace_and_tracks_main():
    assert SPEC["destination"]["namespace"] == "ai-obs"
    assert SPEC["source"]["targetRevision"] == "main"
    assert SPEC["source"]["repoURL"].startswith("https://github.com/") and SPEC["source"]["repoURL"].endswith(".git")


def test_gitops_sync_is_automatic_with_prune_and_self_heal():
    auto = SPEC["syncPolicy"]["automated"]
    assert auto["prune"] is True and auto["selfHeal"] is True


def test_deleting_the_app_does_not_cascade_delete_the_data():
    """No resources-finalizer: removing the Argo app must never wipe the databases' volumes."""
    assert "finalizers" not in APP["metadata"]


def test_no_secret_is_stored_in_the_application():
    text = (ROOT / "deploy" / "argocd" / "application.yaml").read_text().lower()
    for bad in ("password", "token:", "secretkey", "apikey"):
        assert bad not in text.replace("secrets are deliberately", "")
