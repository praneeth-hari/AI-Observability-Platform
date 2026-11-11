"""Tests for scripts/load_test.py. A local HTTPS server plays the collector; no Docker needed."""
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import load_test as lt  # noqa: E402
from make_certs import make_certs  # noqa: E402

KEY = "load-test-key-0123456789"


class FakeCollector:
    """Accepts traces when the Bearer key is right; fail_every=N makes every Nth request return 503."""

    def __init__(self, certdir, fail_every=0):
        self.received, self.count, self.lock = [], 0, threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.lock:
                    outer.count += 1
                    n = outer.count
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    code = 401
                elif fail_every and n % fail_every == 0:
                    code = 503
                else:
                    code = 200
                    req = ExportTraceServiceRequest()
                    req.ParseFromString(body)
                    with outer.lock:
                        outer.received.append(req)
                self.send_response(code)
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

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def certs(tmp_path):
    return make_certs(tmp_path / "certs")


def test_request_is_one_linked_trace_with_a_model_step():
    req = lt.build_request(3, spans_per_trace=5, run_id="abc")
    spans = req.resource_spans[0].scope_spans[0].spans
    assert len(spans) == 5 and len({s.trace_id for s in spans}) == 1
    assert all(s.parent_span_id == spans[0].span_id for s in spans[1:])
    attrs = {a.key: a.value for a in spans[-1].attributes}
    assert attrs["langfuse.observation.type"].string_value == "generation"
    assert attrs["gen_ai.usage.input_tokens"].int_value == 40
    res = {a.key: a.value.string_value for a in req.resource_spans[0].resource.attributes}
    assert res["team"] == "load-test"


def test_each_request_gets_new_ids():
    a = lt.build_request(1).resource_spans[0].scope_spans[0].spans[0]
    b = lt.build_request(2).resource_spans[0].scope_spans[0].spans[0]
    assert a.trace_id != b.trace_id and a.span_id != b.span_id


def test_percentile_and_summary():
    assert lt.percentile([1, 2, 3, 4, 5], 50) == 3
    assert lt.percentile([], 95) == 0.0
    s = lt.summarize([(200, 0.1), (200, 0.2), (503, 0.05), ("ConnectionError", 0.0)], duration=2.0)
    assert s["sent"] == 4 and s["accepted"] == 2 and s["failed"] == 2
    assert s["errors"] == {"503": 1, "ConnectionError": 1}
    assert s["error_rate"] == 0.5 and s["per_second"] == 1.0
    assert 100 <= s["latency_ms"]["p50"] <= 200


def test_summary_of_nothing_does_not_crash():
    s = lt.summarize([], duration=0)
    assert s["sent"] == 0 and s["error_rate"] == 0.0 and s["per_second"] == 0.0


def test_parse_metrics_adds_up_label_sets():
    text = ('# HELP x\n'
            'otelcol_receiver_accepted_spans{receiver="otlp",transport="http"} 10\n'
            'otelcol_receiver_accepted_spans{receiver="otlp",transport="grpc"} 5\n'
            'otelcol_exporter_sent_spans 12.0\n'
            'something_else 99\n')
    t = lt.parse_metrics(text)
    assert t["otelcol_receiver_accepted_spans"] == 15 and t["otelcol_exporter_sent_spans"] == 12
    assert t["otelcol_exporter_send_failed_spans"] == 0


def test_all_traces_arrive(certs):
    c = FakeCollector(certs)
    try:
        stats = lt.run(c.url, KEY, str(certs / "ca.crt"), traces=40, workers=4, spans=3)
    finally:
        c.stop()
    assert stats["accepted"] == 40 and stats["failed"] == 0
    assert len(c.received) == 40
    assert all(len(r.resource_spans[0].scope_spans[0].spans) == 3 for r in c.received)


def test_failures_are_counted_not_hidden(certs):
    c = FakeCollector(certs, fail_every=4)
    try:
        stats = lt.run(c.url, KEY, str(certs / "ca.crt"), traces=40, workers=2)
    finally:
        c.stop()
    assert stats["failed"] == 10 and stats["errors"] == {"503": 10}
    assert stats["error_rate"] == pytest.approx(0.25)


def test_wrong_key_is_reported_as_401(certs):
    c = FakeCollector(certs)
    try:
        stats = lt.run(c.url, "wrong-key", str(certs / "ca.crt"), traces=5, workers=1)
    finally:
        c.stop()
    assert stats["accepted"] == 0 and stats["errors"] == {"401": 5}


def test_unreachable_collector_reports_errors_instead_of_crashing(certs):
    stats = lt.run("https://localhost:1", KEY, str(certs / "ca.crt"), traces=3, workers=1, timeout=2)
    assert stats["accepted"] == 0 and stats["failed"] == 3


def test_main_exit_code_follows_the_error_rate(certs, monkeypatch, capsys):
    for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    good, bad = FakeCollector(certs), FakeCollector(certs, fail_every=2)
    try:
        args = ["--key", KEY, "--ca", str(certs / "ca.crt"), "--traces", "10", "--workers", "2"]
        assert lt.main(args + ["--endpoint", good.url]) == 0
        assert "PASS" in capsys.readouterr().out
        assert lt.main(args + ["--endpoint", bad.url]) == 1
        assert "FAIL" in capsys.readouterr().out
    finally:
        good.stop()
        bad.stop()


@pytest.mark.parametrize("args", [["--endpoint", "http://x:4318"], ["--traces", "0"], ["--workers", "999"]])
def test_unsafe_or_silly_settings_are_refused(args, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "k")
    with pytest.raises(SystemExit):
        lt.main(args)
