"""
Tests for the Helm chart (deploy/helm/ai-observability). They render the chart with the real `helm`
binary and check the Kubernetes objects it produces - no cluster needed.
Needs `helm` on PATH (or HELM_BIN). Locally the tests are skipped if it is missing; CI installs it.
"""
import collections
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy" / "helm" / "ai-observability"
sys.path.insert(0, str(ROOT / "scripts"))
import retention  # noqa: E402
import secrets_tool  # noqa: E402

HELM = os.getenv("HELM_BIN") or shutil.which("helm")
if not HELM:
    if os.getenv("CI"):
        pytest.fail("helm must be installed in CI")
    pytest.skip("helm not installed", allow_module_level=True)


def render(*args, values_file=None):
    cmd = [HELM, "template", "aiobs", str(CHART), *args]
    if values_file:
        cmd += ["-f", str(values_file)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p


@pytest.fixture(scope="module")
def docs():
    p = render()
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def of_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def containers(docs):
    out = []
    for d in docs:
        spec = d.get("spec", {})
        tpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template")
        if tpl:
            out += [(d["metadata"]["name"], c) for c in tpl["spec"]["containers"]]
    return out


def test_lint_passes():
    p = subprocess.run([HELM, "lint", str(CHART), "--strict"], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_expected_workloads_exist(docs):
    names = {d["metadata"]["name"].replace("aiobs-ai-observability-", "") for d in docs
             if d["kind"] in ("Deployment", "StatefulSet", "CronJob")}
    assert names == {"collector", "langfuse-web", "langfuse-worker", "postgres", "clickhouse",
                     "redis", "minio", "retention"}


def test_values_kind_renders_too():
    assert render(values_file=CHART / "values-kind.yaml").returncode == 0


def test_every_container_has_resource_limits_and_a_probe(docs):
    for name, c in containers(docs):
        assert c.get("resources", {}).get("limits", {}).get("memory"), f"{name}: no memory limit"
        if "retention" not in name:
            assert "readinessProbe" in c, f"{name}: no readiness probe"


# Chainguard's free MinIO image only publishes ":latest" (version tags are a paid feature) and we
# cannot look up its digest offline. Known gap: pin it by digest (image@sha256:...) once we can.
UNPINNED_EXCEPTIONS = {"cgr.dev/chainguard/minio"}


def test_all_images_are_pinned_not_latest(docs):
    for name, c in containers(docs):
        image = c["image"]
        if image in UNPINNED_EXCEPTIONS:
            continue
        assert ":" in image.split("/")[-1] and not image.endswith(":latest"), f"{name}: unpinned image {image}"


def test_the_only_unpinned_image_is_the_documented_exception(docs):
    unpinned = {c["image"] for _, c in containers(docs) if ":" not in c["image"].split("/")[-1]}
    assert unpinned <= UNPINNED_EXCEPTIONS


def test_no_secret_value_appears_in_the_rendered_chart(docs):
    """Every password/key must be a secretKeyRef, never a literal in the manifests."""
    secret_names = set(secrets_tool.SECRETS)
    for name, c in containers(docs):
        for e in c.get("env", []):
            if e["name"] in secret_names:
                assert "valueFrom" in e and "value" not in e, f"{name}: {e['name']} is a plain value"
    assert not of_kind(docs, "Secret"), "the chart must not create Secrets itself"


def test_secret_env_references_point_at_the_secrets_tool_keys(docs):
    keys = set(secrets_tool.SECRETS)
    used = set()
    for name, c in containers(docs):
        for e in c.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                assert ref["name"] == "ai-obs-secrets"
                assert ref["key"] in keys, f"{name}: unknown secret key {ref['key']}"
                used.add(ref["key"])
    assert used == keys, f"secrets never used by any pod: {keys - used}"


def test_env_variable_references_are_defined_first_and_not_duplicated(docs):
    """Kubernetes only expands $(VAR) if VAR is defined EARLIER in the same env list."""
    for name, c in containers(docs):
        names = [e["name"] for e in c.get("env", [])]
        dup = [n for n, k in collections.Counter(names).items() if k > 1]
        assert not dup, f"{name}: duplicate env {dup}"
        for i, e in enumerate(c.get("env", [])):
            for ref in re.findall(r"\$\((\w+)\)", e.get("value", "")):
                assert ref in names[:i], f"{name}: {e['name']} uses $({ref}) before it is defined"
        for a in c.get("args", []):
            for ref in re.findall(r"\$\((\w+)\)", a):
                assert ref in names, f"{name}: arg uses undefined $({ref})"


def test_chart_requires_a_secret_name():
    p = render("--set", "secrets.existingSecret=")
    assert p.returncode != 0 and "existingSecret" in p.stderr


def test_chart_collector_config_is_identical_to_the_compose_one():
    """One source of truth: redaction/auth/TLS rules must be the same in Compose and Kubernetes."""
    assert (CHART / "files" / "collector-config.yaml").read_text() == \
        (ROOT / "collector" / "config.yaml").read_text(), \
        "run: cp collector/config.yaml deploy/helm/ai-observability/files/collector-config.yaml"


def test_collector_is_hardened_and_uses_tls_secret(docs):
    dep = next(d for d in of_kind(docs, "Deployment") if d["metadata"]["name"].endswith("-collector"))
    pod = dep["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert {v["name"] for v in pod["volumes"]} == {"config", "tls"}
    assert next(v for v in pod["volumes"] if v["name"] == "tls")["secret"]["secretName"] == "ai-obs-collector-tls"
    assert dep["spec"]["template"]["metadata"]["annotations"]["checksum/config"]


def test_collector_points_at_langfuse_inside_the_cluster(docs):
    dep = next(d for d in of_kind(docs, "Deployment") if d["metadata"]["name"].endswith("-collector"))
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LANGFUSE_OTLP_ENDPOINT"] == "http://aiobs-ai-observability-langfuse-web:3000/api/public/otel"
    svc = {s["metadata"]["name"] for s in of_kind(docs, "Service")}
    assert "aiobs-ai-observability-langfuse-web" in svc


def test_every_service_selects_real_pods(docs):
    """A typo in a label selector would give a Service with no pods behind it."""
    pod_labels = []
    for d in docs:
        tpl = d.get("spec", {}).get("template")
        if tpl:
            pod_labels.append(tpl["metadata"]["labels"])
    for s in of_kind(docs, "Service"):
        sel = s["spec"]["selector"]
        assert any(all(l.get(k) == v for k, v in sel.items()) for l in pod_labels), s["metadata"]["name"]
    for d in docs:   # a workload's selector must match its own pod labels or Kubernetes rejects it
        if d["kind"] in ("Deployment", "StatefulSet"):
            sel = d["spec"]["selector"]["matchLabels"]
            labels = d["spec"]["template"]["metadata"]["labels"]
            assert all(labels.get(k) == v for k, v in sel.items()), d["metadata"]["name"]


def test_statefulsets_have_persistent_storage(docs):
    for d in of_kind(docs, "StatefulSet"):
        assert d["spec"]["volumeClaimTemplates"], d["metadata"]["name"]


def test_langfuse_s3_prefixes_match_the_retention_rules(docs):
    web = next(d for d in of_kind(docs, "Deployment") if d["metadata"]["name"].endswith("langfuse-web"))
    env = {e["name"]: e.get("value") for e in web["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (env["LANGFUSE_S3_EVENT_UPLOAD_PREFIX"], env["LANGFUSE_S3_MEDIA_UPLOAD_PREFIX"]) == \
        tuple(retention.MINIO_PREFIXES)


def test_retention_tables_match_scripts_retention(docs):
    job = next(d for d in of_kind(docs, "CronJob"))
    script = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
    found = dict(re.findall(r'"(\w+):(\w+)"', script))
    assert found == retention.TABLES
    assert "INTERVAL 30 DAY" in script


@pytest.mark.parametrize("bad", ["0", "-5", "99999"])
def test_retention_days_are_validated(bad):
    p = render("--set", f"retention.days={bad}")
    assert p.returncode != 0 and "retention.days" in p.stderr


def test_retention_can_be_disabled():
    p = render("--set", "retention.enabled=false")
    assert p.returncode == 0 and "CronJob" not in p.stdout


def test_network_policies_isolate_the_databases(docs):
    pols = {p["metadata"]["name"].replace("aiobs-ai-observability-", ""): p for p in of_kind(docs, "NetworkPolicy")}
    assert "default-deny-ingress" in pols
    for store in ("postgres", "clickhouse", "redis", "minio"):
        allowed = pols[f"allow-{store}"]["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"][0]["values"]
        assert "collector" not in allowed and "langfuse-web" in allowed
    # agents must be able to reach the collector from outside the namespace
    assert "from" not in pols["allow-collector"]["spec"]["ingress"][0]
    assert {p["port"] for p in pols["allow-collector"]["spec"]["ingress"][0]["ports"]} == {4317, 4318}


def test_network_policy_can_be_turned_off():
    assert "NetworkPolicy" not in render("--set", "networkPolicy.enabled=false").stdout


def test_ingress_is_off_by_default_and_works_when_enabled(docs):
    assert not of_kind(docs, "Ingress")
    p = render("--set", "langfuse.ingress.enabled=true", "--set", "langfuse.ingress.host=obs.corp.example")
    assert p.returncode == 0
    ing = next(d for d in yaml.safe_load_all(p.stdout) if d and d["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "obs.corp.example"


def test_langfuse_web_listens_on_all_interfaces(docs):
    """Regression: without HOSTNAME=0.0.0.0 the UI is unreachable through `kubectl port-forward`
    (found on a real kind cluster: 'connection refused' inside the pod)."""
    web = next(d for d in of_kind(docs, "Deployment") if d["metadata"]["name"].endswith("langfuse-web"))
    env = {e["name"]: e.get("value") for e in web["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HOSTNAME"] == "0.0.0.0"


def _mib(q):
    n, unit = re.fullmatch(r"(\d+)(Mi|Gi)", str(q)).groups()
    return int(n) * (1024 if unit == "Gi" else 1)


@pytest.mark.parametrize("values", [None, CHART / "values-kind.yaml"])
def test_node_heap_is_below_the_memory_limit_for_langfuse(values):
    """Regression: with a 1Gi limit Node's default heap (~500MB) was too small and Langfuse crashed with
    'JavaScript heap out of memory'. The heap must be big enough (>=1GB web) yet below the pod limit."""
    p = render(values_file=values)
    assert p.returncode == 0, p.stderr
    for d in (d for d in yaml.safe_load_all(p.stdout) if d and d["kind"] == "Deployment"):
        if "langfuse" not in d["metadata"]["name"]:
            continue
        c = d["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        heap = int(re.search(r"max-old-space-size=(\d+)", env["NODE_OPTIONS"]).group(1))
        limit = _mib(c["resources"]["limits"]["memory"])
        assert heap <= limit * 0.75, f"{d['metadata']['name']}: heap {heap}MiB too close to limit {limit}MiB"
        if d["metadata"]["name"].endswith("web"):
            assert heap >= 1024, "web needs a heap of at least 1GiB"


# ---- optional monitoring (Prometheus + Grafana) ----

def test_monitoring_is_off_by_default(docs):
    names = {d["metadata"]["name"] for d in docs}
    assert not any("prometheus" in n or "grafana" in n for n in names)


@pytest.fixture(scope="module")
def mon():
    p = render("--set", "monitoring.enabled=true")
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def test_monitoring_adds_prometheus_and_grafana(mon):
    deps = {d["metadata"]["name"].replace("aiobs-ai-observability-", "") for d in of_kind(mon, "Deployment")}
    assert {"prometheus", "grafana"} <= deps


def test_monitoring_containers_are_pinned_limited_and_probed(mon):
    for name, c in containers(mon):
        if "prometheus" in name or "grafana" in name:
            assert not c["image"].endswith(":latest") and ":" in c["image"]
            assert c["resources"]["limits"]["memory"]
            assert "readinessProbe" in c
            assert c["securityContext"]["allowPrivilegeEscalation"] is False


def test_prometheus_scrapes_the_collector_metrics_port(mon):
    cm = next(d for d in of_kind(mon, "ConfigMap") if d["metadata"]["name"].endswith("prometheus-config"))
    cfg = yaml.safe_load(cm["data"]["prometheus.yml"])
    target = cfg["scrape_configs"][0]["static_configs"][0]["targets"][0]
    svc = next(d for d in of_kind(mon, "Service") if d["metadata"]["name"].endswith("-collector"))
    assert target == f'{svc["metadata"]["name"]}:8888'
    assert any(p["port"] == 8888 for p in svc["spec"]["ports"])
    assert yaml.safe_load(cm["data"]["rules.yaml"])["groups"]


def test_grafana_password_comes_from_the_secret(mon):
    g = next(d for d in of_kind(mon, "Deployment") if d["metadata"]["name"].endswith("grafana"))
    env = {e["name"]: e for e in g["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["GF_SECURITY_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "LANGFUSE_INIT_USER_PASSWORD" and ref["name"] == "ai-obs-secrets"


def test_grafana_dashboard_and_datasource_are_provisioned(mon):
    cm = next(d for d in of_kind(mon, "ConfigMap") if d["metadata"]["name"].endswith("grafana-config"))
    assert {"datasource.yaml", "dashboards.yaml", "collector-health.json"} <= set(cm["data"])
    ds = yaml.safe_load(cm["data"]["datasource.yaml"])["datasources"][0]
    assert ds["uid"] == "prometheus" and ds["url"].endswith("-prometheus:9090")


def test_network_policies_cover_monitoring(mon):
    pols = {p["metadata"]["name"] for p in of_kind(mon, "NetworkPolicy")}
    assert any(n.endswith("allow-prometheus") for n in pols) and any(n.endswith("allow-grafana") for n in pols)


# ---- optional cert-manager certificate ----

def test_cert_manager_is_off_by_default(docs):
    assert not of_kind(docs, "Certificate") and not of_kind(docs, "Issuer")


@pytest.fixture(scope="module")
def cm():
    p = render("--set", "certManager.enabled=true")
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def test_cert_manager_makes_a_private_ca_and_a_server_certificate(cm):
    certs = {c["metadata"]["name"].replace("aiobs-ai-observability-", ""): c for c in of_kind(cm, "Certificate")}
    issuers = {i["metadata"]["name"].replace("aiobs-ai-observability-", ""): i for i in of_kind(cm, "Issuer")}
    assert set(certs) == {"ca", "collector-tls"} and set(issuers) == {"selfsigned", "ca-issuer"}
    assert certs["ca"]["spec"]["isCA"] is True and not certs["collector-tls"]["spec"].get("isCA")
    assert certs["collector-tls"]["spec"]["issuerRef"]["name"].endswith("ca-issuer")
    assert issuers["ca-issuer"]["spec"]["ca"]["secretName"] == certs["ca"]["spec"]["secretName"]
    assert certs["collector-tls"]["spec"]["usages"] == ["server auth"]


def test_certificate_covers_the_names_agents_use(cm):
    leaf = next(c for c in of_kind(cm, "Certificate") if c["metadata"]["name"].endswith("collector-tls"))
    names = leaf["spec"]["dnsNames"]
    assert "localhost" in names and any(n.endswith("-collector.default.svc") for n in names)


def test_collector_mounts_the_cert_manager_secret_with_the_expected_file_names(cm):
    dep = next(d for d in of_kind(cm, "Deployment") if d["metadata"]["name"].endswith("-collector"))
    vol = next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "tls")["secret"]
    leaf = next(c for c in of_kind(cm, "Certificate") if c["metadata"]["name"].endswith("collector-tls"))
    assert vol["secretName"] == leaf["spec"]["secretName"]
    assert {i["path"] for i in vol["items"]} == {"server.crt", "server.key", "ca.crt"}


def test_collector_reloads_a_renewed_certificate():
    text = (ROOT / "collector" / "config.yaml").read_text()
    assert text.count("reload_interval") == 2
