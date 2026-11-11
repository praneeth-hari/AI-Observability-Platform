"""Checks on the monitoring files that need no helm: alert rules and the Grafana dashboard."""
import json
import pathlib
import re

import yaml

FILES = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "helm" / "ai-observability" / "files"
# Metrics the collector really publishes (checked against its /metrics page, 0.165.0).
KNOWN = {
    "up", "otelcol_exporter_queue_size", "otelcol_exporter_queue_capacity", "otelcol_process_memory_rss",
    "otelcol_process_uptime", "otelcol_receiver_accepted_spans", "otelcol_receiver_refused_spans",
    "otelcol_exporter_sent_spans", "otelcol_exporter_send_failed_spans",
}


def metrics_in(expr):
    return {m for m in re.findall(r"[a-z_]+", expr) if m.startswith("otelcol_") or m == "up"}


def rules():
    doc = yaml.safe_load((FILES / "prometheus-rules.yaml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def test_every_alert_has_expr_severity_and_text():
    rs = rules()
    assert len(rs) >= 5
    for r in rs:
        assert r["expr"] and r["for"]
        assert r["labels"]["severity"] in {"warning", "critical"}
        assert r["annotations"]["summary"] and r["annotations"]["description"]


def test_alerts_only_use_metrics_the_collector_publishes():
    for r in rules():
        assert metrics_in(r["expr"]) <= KNOWN, r["alert"]


def test_the_key_failure_alerts_exist():
    names = {r["alert"] for r in rules()}
    assert {"CollectorDown", "ExportFailing", "ExportQueueFilling"} <= names


def test_dashboard_is_valid_and_uses_known_metrics():
    dash = json.loads((FILES / "grafana-dashboard.json").read_text())
    assert dash["uid"] and dash["panels"]
    for p in dash["panels"]:
        assert p["datasource"]["uid"] == "prometheus"
        for t in p["targets"]:
            assert metrics_in(t["expr"]) <= KNOWN, t["expr"]


def test_delivered_panel_counts_only_the_langfuse_exporter():
    """The collector also has a debug exporter; adding both would show double the real delivery."""
    dash = json.loads((FILES / "grafana-dashboard.json").read_text())
    panel = next(p for p in dash["panels"] if p["title"] == "Spans delivered to Langfuse per second")
    assert 'exporter="otlp_http/langfuse"' in panel["targets"][0]["expr"]
