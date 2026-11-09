"""
Test harness: starts a REAL OpenTelemetry Collector with the project's collector/config.yaml
(on separate ports, so it never clashes with your running docker compose stack) and a fake
"Langfuse" backend that records everything the collector exports.

The collector runs from either:
  - the binary at $OTELCOL_BIN (fast, used in dev containers), or
  - Docker image otel/opentelemetry-collector-contrib (default; needs Docker running).
"""
import gzip
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "collector" / "config.yaml"
IMAGE = "otel/opentelemetry-collector-contrib:0.165.0"

AGENT_KEY = "test-agent-key-0123456789"
GRPC_PORT, HTTP_PORT, HEALTH_PORT, METRICS_PORT, BACKEND_PORT = 14317, 14318, 14133, 14888, 14999


# ---------------- fake Langfuse backend ----------------
def _val(v):
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [_val(x) for x in v.array_value.values]
    return getattr(v, kind) if kind else None


class Backend:
    def __init__(self):
        self.spans, self.requests, self.fail_next = [], 0, 0
        self.lock = threading.Lock()
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                with backend.lock:
                    backend.requests += 1
                    if backend.fail_next > 0:
                        backend.fail_next -= 1
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    req = ExportTraceServiceRequest()
                    req.ParseFromString(body)
                    for rs in req.resource_spans:
                        res = {a.key: _val(a.value) for a in rs.resource.attributes}
                        for ss in rs.scope_spans:
                            for sp in ss.spans:
                                backend.spans.append({
                                    "name": sp.name,
                                    "trace_id": sp.trace_id.hex(),
                                    "span_id": sp.span_id.hex(),
                                    "parent_id": sp.parent_span_id.hex(),
                                    "status_message": sp.status.message,
                                    "attrs": {a.key: _val(a.value) for a in sp.attributes},
                                    "events": [{a.key: _val(a.value) for a in e.attributes} for e in sp.events],
                                    "resource": res,
                                })
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", BACKEND_PORT), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.spans.clear()
            self.requests, self.fail_next = 0, 0

    def everything(self) -> str:
        """All received data flattened to one string (for leak checks)."""
        with self.lock:
            return repr(self.spans)

    def wait_for(self, predicate, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                hit = [s for s in self.spans if predicate(s)]
            if hit:
                return hit
            time.sleep(0.3)
        raise AssertionError(f"timed out after {timeout}s waiting for spans; received so far: "
                             f"{[s['name'] for s in self.spans]}")


# ---------------- collector under test ----------------
class Collector:
    def __init__(self, tmpdir):
        self.mode = "binary" if os.getenv("OTELCOL_BIN") else "docker"
        text = CONFIG.read_text(encoding="utf-8")
        for old, new in [("0.0.0.0:4317", f"0.0.0.0:{GRPC_PORT}"), ("0.0.0.0:4318", f"0.0.0.0:{HTTP_PORT}"),
                         ("0.0.0.0:13133", f"0.0.0.0:{HEALTH_PORT}"), ("port: 8888", f"port: {METRICS_PORT}")]:
            assert old in text, f"expected '{old}' in collector/config.yaml"
            text = text.replace(old, new)
        self.config = Path(tmpdir) / "collector-test.yaml"
        self.config.write_text(text, encoding="utf-8")
        host = "127.0.0.1" if self.mode == "binary" else "host.docker.internal"
        self.env = {"LANGFUSE_OTLP_ENDPOINT": f"http://{host}:{BACKEND_PORT}/api/public/otel",
                    "LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test",
                    "DEPLOYMENT_ENV": "test", "AGENT_API_KEY": AGENT_KEY}
        self.proc = None
        self.name = "aiobs-test-collector"

    http_url = f"http://127.0.0.1:{HTTP_PORT}"
    health_url = f"http://127.0.0.1:{HEALTH_PORT}/"

    def start(self):
        if self.mode == "binary":
            self.proc = subprocess.Popen([os.environ["OTELCOL_BIN"], f"--config={self.config}"],
                                         env={**os.environ, **self.env},
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            cmd = ["docker", "run", "-d", "--name", self.name,
                   "--add-host", "host.docker.internal:host-gateway",
                   "-p", f"{GRPC_PORT}:{GRPC_PORT}", "-p", f"{HTTP_PORT}:{HTTP_PORT}",
                   "-p", f"{HEALTH_PORT}:{HEALTH_PORT}",
                   "--mount", f"type=bind,source={self.config},target=/etc/otelcol/config.yaml,readonly"]
            for k, v in self.env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += [IMAGE, "--config=/etc/otelcol/config.yaml"]
            out = subprocess.run(cmd, capture_output=True, text=True)
            assert out.returncode == 0, f"docker run failed: {out.stderr}"
        self.wait_ready()

    def stop(self):
        if self.mode == "binary" and self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=20)
            self.proc = None
        elif self.mode == "docker":
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def restart(self):
        self.stop()
        self.start()

    def wait_ready(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if requests.get(self.health_url, timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise AssertionError("collector did not become healthy")

    def healthy(self) -> bool:
        try:
            return requests.get(self.health_url, timeout=2).status_code == 200
        except requests.RequestException:
            return False


# ---------------- fixtures ----------------
@pytest.fixture(scope="session")
def backend():
    return Backend()


@pytest.fixture(scope="session")
def collector(backend):
    if not os.getenv("OTELCOL_BIN") and not shutil.which("docker"):
        pytest.fail("Need Docker, or set OTELCOL_BIN to an otelcol-contrib binary")
    tmp = tempfile.mkdtemp()
    c = Collector(tmp)
    c.start()
    yield c
    c.stop()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean(backend):
    backend.reset()
    yield


@pytest.fixture
def send(collector):
    """send(fn, resource_attrs=None, key=AGENT_KEY): run fn(tracer) and export its spans to the collector."""
    def _send(fn, resource_attrs=None, key=AGENT_KEY):
        provider = TracerProvider(resource=Resource.create({"service.name": "pytest-agent", **(resource_attrs or {})}))
        provider.add_span_processor(SimpleSpanProcessor(
            OTLPSpanExporter(endpoint=f"{collector.http_url}/v1/traces", timeout=10,
                             headers={"Authorization": f"Bearer {key}"} if key else {})))
        fn(provider.get_tracer("tests"))
        provider.shutdown()
    return _send
