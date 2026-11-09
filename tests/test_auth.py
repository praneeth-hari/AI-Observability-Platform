"""Security tests: only agents holding the API key may send telemetry."""
import time

import requests

from conftest import AGENT_KEY


def _post_empty_trace(collector, headers):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    return requests.post(f"{collector.http_url}/v1/traces", data=ExportTraceServiceRequest().SerializeToString(),
                         headers={"Content-Type": "application/x-protobuf", **headers}, timeout=10)


def test_missing_key_is_rejected(collector):
    assert _post_empty_trace(collector, {}).status_code == 401


def test_wrong_key_is_rejected(collector):
    assert _post_empty_trace(collector, {"Authorization": "Bearer not-the-key"}).status_code == 401


def test_key_without_bearer_prefix_is_rejected(collector):
    assert _post_empty_trace(collector, {"Authorization": AGENT_KEY}).status_code == 401


def test_correct_key_is_accepted(collector):
    assert _post_empty_trace(collector, {"Authorization": f"Bearer {AGENT_KEY}"}).status_code == 200


def test_unauthenticated_spans_never_reach_the_backend(send, backend):
    send(lambda t: t.start_span("intruder").end(), key="wrong-key")
    time.sleep(8)  # longer than the collector's batch timeout
    assert not [s for s in backend.spans if s["name"] == "intruder"]


def test_authenticated_spans_are_delivered(send, backend):
    send(lambda t: t.start_span("legit").end())
    backend.wait_for(lambda s: s["name"] == "legit")


def test_health_endpoint_stays_open_for_probes(collector):
    """Kubernetes/monitoring probes can't send agent keys."""
    assert requests.get(collector.health_url, timeout=5).status_code == 200
