"""Pipeline tests: trace completeness, enrichment, malformed input, outage retry, restart."""
import requests

from conftest import AGENT_KEY

AUTH = {"Authorization": f"Bearer {AGENT_KEY}"}


def test_trace_is_complete_and_correlated(send, backend):
    def fn(tracer):
        with tracer.start_as_current_span("agent.run"):
            with tracer.start_as_current_span("llm.planner"):
                pass
            with tracer.start_as_current_span("tool.calculator"):
                pass
            with tracer.start_as_current_span("llm.answer"):
                pass
    send(fn)
    spans = backend.wait_for(lambda s: s["name"] == "llm.answer")
    assert len(spans) == 1
    import time; time.sleep(1)  # let the whole batch land
    got = {s["name"]: s for s in backend.spans}
    assert set(got) == {"agent.run", "llm.planner", "tool.calculator", "llm.answer"}
    assert len({s["trace_id"] for s in got.values()}) == 1, "all spans must share one trace id"
    root = got["agent.run"]
    assert root["parent_id"] == "", "root must have no parent"
    for child in ("llm.planner", "tool.calculator", "llm.answer"):
        assert got[child]["parent_id"] == root["span_id"], f"{child} must be a child of agent.run"


def test_spans_are_enriched_by_gateway(send, backend):
    send(lambda t: t.start_span("enrich").end())
    span = backend.wait_for(lambda s: s["name"] == "enrich")[0]
    assert span["resource"]["deployment.environment"] == "test"
    assert span["resource"]["telemetry.gateway"] == "ai-obs-collector"


def test_malformed_payload_is_rejected_and_collector_survives(collector, send, backend):
    r = requests.post(f"{collector.http_url}/v1/traces", data=b"this is not protobuf",
                      headers={"Content-Type": "application/x-protobuf", **AUTH}, timeout=10, verify=collector.ca)
    assert 400 <= r.status_code < 500, f"expected a 4xx rejection, got {r.status_code}"
    r = requests.post(f"{collector.http_url}/v1/traces", data=b"{not json",
                      headers={"Content-Type": "application/json", **AUTH}, timeout=10, verify=collector.ca)
    assert 400 <= r.status_code < 500
    assert collector.healthy()
    send(lambda t: t.start_span("after-garbage").end())
    backend.wait_for(lambda s: s["name"] == "after-garbage")


def test_retry_after_backend_outage(send, backend):
    """Backend returns 503 twice; the collector must retry and still deliver the span."""
    backend.fail_next = 2
    send(lambda t: t.start_span("retried").end())
    backend.wait_for(lambda s: s["name"] == "retried", timeout=90)
    assert backend.requests >= 3, "expected at least 2 failed attempts plus a success"


def test_collector_restart(collector, send, backend):
    send(lambda t: t.start_span("before-restart").end())
    backend.wait_for(lambda s: s["name"] == "before-restart")
    collector.restart()
    assert collector.healthy()
    send(lambda t: t.start_span("after-restart").end())
    backend.wait_for(lambda s: s["name"] == "after-restart")
