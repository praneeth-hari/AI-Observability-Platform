"""
Load test for the collector: sends many traces and reports how fast and how reliably they were accepted.

  python scripts/load_test.py --traces 200 --workers 4
  python scripts/load_test.py --traces 1000 --workers 8 --rate 50          # at most 50 requests per second
  python scripts/load_test.py --traces 200 --metrics-url http://localhost:8888/metrics   # also show collector counters

Settings come from the same environment variables the agents use (flags override them):
  OTEL_EXPORTER_OTLP_ENDPOINT   e.g. https://localhost:4318
  AGENT_API_KEY                 the agent key
  AGENT_CA_CERT                 CA file for the collector's certificate (e.g. certs\\k8s-ca.crt)

Each request carries ONE trace (an agent run with a few steps), so "traces per second" = requests per second.
The traces are labelled team=load-test and trace name "load-test", so they are easy to find and ignore.

How to read the result: "accepted" means the collector answered 200. This is the gateway's own speed.
If you go through `kubectl port-forward`, the forward itself is often the bottleneck on a laptop,
so treat the numbers as a floor, not as the platform's ceiling. Exit code is 1 when the error rate
is above --max-error-rate, so the test can gate a release.
"""
import argparse
import os
import random
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip().strip('"').strip("'"))
    except OSError:
        pass


def _attr(span_or_res, key, value):
    a = span_or_res.attributes.add()
    a.key = key
    if isinstance(value, int):
        a.value.int_value = value
    else:
        a.value.string_value = str(value)


def build_request(index, spans_per_trace=5, run_id="run"):
    """One ExportTraceServiceRequest holding one trace: a root span plus child steps."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    _attr(rs.resource, "service.name", "load-test")
    _attr(rs.resource, "team", "load-test")
    _attr(rs.resource, "agent.platform", "load-test")
    ss = rs.scope_spans.add()
    ss.scope.name = "load-test"
    trace_id = random.randbytes(16)
    root_id = random.randbytes(8)
    start = time.time_ns()
    for n in range(spans_per_trace):
        sp = ss.spans.add()
        sp.trace_id = trace_id
        sp.span_id = root_id if n == 0 else random.randbytes(8)
        if n:
            sp.parent_span_id = root_id
        sp.name = "load-test" if n == 0 else ("llm.answer" if n == spans_per_trace - 1 else f"step-{n}")
        sp.start_time_unix_nano = start + n * 1_000_000
        sp.end_time_unix_nano = start + (n + 1) * 1_000_000 + 5_000_000
        if n == 0:
            _attr(sp, "langfuse.trace.name", "load-test")
            _attr(sp, "langfuse.trace.input", f"load test {run_id} #{index}")
            _attr(sp, "langfuse.observation.input", f"load test {run_id} #{index}")
            _attr(sp, "langfuse.trace.output", "ok")
            _attr(sp, "langfuse.observation.output", "ok")
        elif n == spans_per_trace - 1:
            _attr(sp, "langfuse.observation.type", "generation")
            _attr(sp, "gen_ai.request.model", "fake-llm")
            _attr(sp, "gen_ai.usage.input_tokens", 40)
            _attr(sp, "gen_ai.usage.output_tokens", 12)
    return req


def percentile(sorted_values, p):
    if not sorted_values:
        return 0.0
    k = min(len(sorted_values) - 1, max(0, int(round(p / 100 * (len(sorted_values) - 1)))))
    return sorted_values[k]


def summarize(results, duration):
    """results: list of (http_status_or_error_text, seconds)."""
    total = len(results)
    ok = [r for r in results if r[0] == 200]
    errors = {}
    for status, _ in results:
        if status != 200:
            errors[str(status)] = errors.get(str(status), 0) + 1
    lat = sorted(r[1] for r in ok)
    return {
        "sent": total, "accepted": len(ok), "failed": total - len(ok), "errors": errors,
        "error_rate": (total - len(ok)) / total if total else 0.0,
        "seconds": duration, "per_second": (len(ok) / duration) if duration > 0 else 0.0,
        "latency_ms": {"p50": percentile(lat, 50) * 1000, "p95": percentile(lat, 95) * 1000,
                       "p99": percentile(lat, 99) * 1000, "max": (lat[-1] * 1000) if lat else 0.0,
                       "mean": (statistics.mean(lat) * 1000) if lat else 0.0},
    }


COUNTERS = ("otelcol_receiver_accepted_spans", "otelcol_receiver_refused_spans",
            "otelcol_exporter_sent_spans", "otelcol_exporter_send_failed_spans")


def parse_metrics(text):
    """Add up each interesting counter over all its label sets (Prometheus text format)."""
    totals = {name: 0.0 for name in COUNTERS}
    for line in text.splitlines():
        m = re.match(r"^(\w+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", line)
        if m and m.group(1) in totals:
            totals[m.group(1)] += float(m.group(2))
    return totals


def fetch_metrics(url):
    try:
        return parse_metrics(requests.get(url, timeout=5).text)
    except requests.RequestException:
        return None


def run(endpoint, key, ca=None, traces=200, workers=4, spans=5, rate=0.0, timeout=15):
    url = endpoint.rstrip("/") + "/v1/traces"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-protobuf"}
    verify = ca if ca else True
    run_id = format(random.getrandbits(24), "06x")
    local = threading.local()
    results = []
    lock = threading.Lock()
    gap = (workers / rate) if rate > 0 else 0.0     # each worker waits this long between requests

    def one(i):
        if not hasattr(local, "session"):
            local.session = requests.Session()
        body = build_request(i, spans, run_id).SerializeToString()
        t0 = time.perf_counter()
        try:
            status = local.session.post(url, data=body, headers=headers, verify=verify, timeout=timeout).status_code
        except requests.RequestException as e:
            status = type(e).__name__
        took = time.perf_counter() - t0
        with lock:
            results.append((status, took))
        if gap:
            time.sleep(max(0.0, gap - took))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(traces)))
    return summarize(results, time.perf_counter() - start)


def report(stats, before=None, after=None):
    lat = stats["latency_ms"]
    lines = [
        f"Traces sent:      {stats['sent']}",
        f"Accepted (200):   {stats['accepted']}",
        f"Failed:           {stats['failed']}  {stats['errors'] or ''}".rstrip(),
        f"Error rate:       {stats['error_rate']:.2%}",
        f"Duration:         {stats['seconds']:.1f} s",
        f"Throughput:       {stats['per_second']:.1f} traces/s",
        f"Latency (ms):     p50 {lat['p50']:.0f}   p95 {lat['p95']:.0f}   p99 {lat['p99']:.0f}   max {lat['max']:.0f}",
    ]
    if before and after:
        lines.append("Collector counters during the test (spans):")
        for name in COUNTERS:
            lines.append(f"  {name.replace('otelcol_', ''):32s} +{after[name] - before[name]:.0f}")
    return "\n".join(lines)


def main(argv=None):
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://localhost:4318"))
    ap.add_argument("--key", default=os.getenv("AGENT_API_KEY", ""))
    ap.add_argument("--ca", default=os.getenv("AGENT_CA_CERT") or str(ROOT / "certs" / "ca.crt"))
    ap.add_argument("--traces", type=int, default=200)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--spans", type=int, default=5, help="spans (steps) per trace")
    ap.add_argument("--rate", type=float, default=0.0, help="max requests per second (0 = as fast as possible)")
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--metrics-url", help="collector metrics URL, e.g. http://localhost:8888/metrics")
    a = ap.parse_args(argv)
    if not a.key:
        sys.exit("No agent key: set AGENT_API_KEY or pass --key.")
    if not a.endpoint.startswith("https://"):
        sys.exit("The endpoint must start with https:// (the key must not travel unencrypted).")
    if not (1 <= a.traces <= 100000 and 1 <= a.workers <= 64 and 1 <= a.spans <= 50):
        sys.exit("Use --traces 1..100000, --workers 1..64, --spans 1..50.")
    print(f"Sending {a.traces} traces ({a.spans} steps each) with {a.workers} workers to {a.endpoint} ...")
    before = fetch_metrics(a.metrics_url) if a.metrics_url else None
    stats = run(a.endpoint, a.key, a.ca, a.traces, a.workers, a.spans, a.rate)
    if a.metrics_url:
        time.sleep(6)       # let the collector's batch timer flush before reading its counters
    after = fetch_metrics(a.metrics_url) if a.metrics_url else None
    print(report(stats, before, after))
    ok = stats["error_rate"] <= a.max_error_rate
    print("RESULT:", "PASS" if ok else f"FAIL (error rate above {a.max_error_rate:.2%})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
