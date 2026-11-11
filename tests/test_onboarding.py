"""Tests for the onboarding kit (onboarding/otel_setup.py + the simulated external agent).

They need no collector and no Docker: a local HTTPS server plays the collector, so we can check what
a new team's agent really sends (TLS with a private CA, the Bearer key, the resource labels, the spans).
"""
import gzip
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "onboarding"))
sys.path.insert(0, str(ROOT / "scripts"))
import example_external_agent as agent  # noqa: E402
import otel_setup  # noqa: E402
from make_certs import make_certs  # noqa: E402

KEY = "onboarding-test-key-123456"


@pytest.fixture
def memory():
    exporter = InMemorySpanExporter()
    provider, tracer = otel_setup.setup_tracing("demo-agent", team="team-x", platform="unit-test",
                                                span_processor=SimpleSpanProcessor(exporter))
    yield exporter, tracer
    provider.shutdown()


def by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def test_resource_labels_identify_the_team_and_platform(memory):
    exporter, tracer = memory
    agent.run(tracer, "Where is order 1042?")
    res = exporter.get_finished_spans()[0].resource.attributes
    assert res["service.name"] == "demo-agent" and res["team"] == "team-x" and res["agent.platform"] == "unit-test"


def test_an_agent_run_produces_a_trace_with_tool_and_llm_spans(memory):
    exporter, tracer = memory
    answer, trace_id = agent.run(tracer, "Where is order 1042?")
    spans = by_name(exporter)
    assert set(spans) == {"order-support-agent", "order_lookup", "llm.answer"}
    assert "shipped" in answer and len(trace_id) == 32
    assert len({s.context.trace_id for s in spans.values()}) == 1
    root = spans["order-support-agent"]
    assert spans["order_lookup"].parent.span_id == root.context.span_id
    assert spans["llm.answer"].parent.span_id == root.context.span_id
    assert root.attributes["langfuse.trace.input"] == "Where is order 1042?"
    assert root.attributes["langfuse.trace.output"] == answer


def test_llm_span_carries_model_and_token_counts(memory):
    exporter, tracer = memory
    agent.run(tracer, "Where is order 1042?")
    a = by_name(exporter)["llm.answer"].attributes
    assert a["langfuse.observation.type"] == "generation"
    assert a["gen_ai.request.model"] == "fake-llm"
    assert a["gen_ai.usage.input_tokens"] > 0 and a["gen_ai.usage.output_tokens"] > 0


def test_a_failing_tool_marks_the_trace_as_an_error(memory):
    exporter, tracer = memory
    with pytest.raises(KeyError):
        agent.run(tracer, "Where is order 9999?")
    spans = by_name(exporter)
    assert spans["order_lookup"].attributes["langfuse.observation.level"] == "ERROR"
    assert spans["order-support-agent"].attributes["langfuse.trace.output"].startswith("FAILED")


@pytest.mark.parametrize("env,message", [
    ({}, "OTEL_EXPORTER_OTLP_ENDPOINT, AGENT_API_KEY"),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://x:4318"}, "AGENT_API_KEY"),
    ({"AGENT_API_KEY": "k"}, "OTEL_EXPORTER_OTLP_ENDPOINT"),
])
def test_missing_settings_give_a_clear_error(monkeypatch, env, message):
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "AGENT_API_KEY", "AGENT_CA_CERT"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(otel_setup.ConfigError, match=message):
        otel_setup.setup_tracing("a", team="t")


def test_plain_http_endpoint_is_refused(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    with pytest.raises(otel_setup.ConfigError, match="https"):
        otel_setup.setup_tracing("a", team="t")


# ---- over the wire: a local HTTPS server stands in for the collector ----

class FakeCollector:
    def __init__(self, certdir):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                req = ExportTraceServiceRequest()
                req.ParseFromString(body)
                outer.requests.append({"auth": self.headers.get("Authorization"), "path": self.path, "req": req})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(certdir / "server.crt"), str(certdir / "server.key"))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"https://localhost:{self.server.server_address[1]}"


def test_traces_reach_a_tls_collector_with_the_api_key(tmp_path, monkeypatch):
    certs = make_certs(tmp_path / "certs")
    fake = FakeCollector(certs)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", fake.url)
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    monkeypatch.setenv("AGENT_CA_CERT", str(certs / "ca.crt"))
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    provider, tracer = otel_setup.setup_tracing("demo-agent", team="team-x", platform="unit-test")
    agent.run(tracer, "Where is order 1042?")
    provider.shutdown()                      # flushes the batch
    fake.server.shutdown()
    assert fake.requests, "nothing reached the collector"
    sent = fake.requests[0]
    assert sent["auth"] == f"Bearer {KEY}" and sent["path"] == "/v1/traces"
    spans = [s for rs in sent["req"].resource_spans for ss in rs.scope_spans for s in ss.spans]
    assert {s.name for s in spans} == {"order-support-agent", "order_lookup", "llm.answer"}
    res = {a.key: a.value.string_value for a in sent["req"].resource_spans[0].resource.attributes}
    assert res["team"] == "team-x"


def test_the_template_is_standalone():
    """Teams copy this one file, so it must not import anything from this repository."""
    text = (ROOT / "onboarding" / "otel_setup.py").read_text()
    imports = [l for l in text.splitlines() if l.startswith(("import ", "from "))]
    assert all(l.split()[1].split(".")[0] in {"contextlib", "json", "os", "opentelemetry"} for l in imports)
