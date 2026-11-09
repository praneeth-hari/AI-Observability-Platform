"""Transport security tests: telemetry (and the API key inside it) must be encrypted in transit."""
import tempfile

import pytest
import requests

from conftest import AGENT_KEY, HTTP_PORT
from make_certs import make_certs

AUTH = {"Authorization": f"Bearer {AGENT_KEY}"}


def test_https_with_trusted_ca_works(collector):
    r = requests.post(f"{collector.http_url}/v1/traces", data=b"", headers={"Content-Type": "application/x-protobuf", **AUTH},
                      timeout=10, verify=collector.ca)
    assert r.status_code == 200


def test_plain_http_is_refused(collector):
    """An agent that forgets TLS must not be able to send its key in clear text."""
    try:
        r = requests.post(f"http://localhost:{HTTP_PORT}/v1/traces", data=b"", headers=AUTH, timeout=10)
    except requests.RequestException:
        return  # connection dropped: good
    assert r.status_code >= 400, f"plain HTTP was accepted with status {r.status_code}"


def test_untrusted_ca_is_rejected(collector):
    """A client that does not trust our CA (or is talking to an impostor) must fail the handshake."""
    other_ca = make_certs(tempfile.mkdtemp()) / "ca.crt"
    with pytest.raises(requests.exceptions.SSLError):
        requests.post(f"{collector.http_url}/v1/traces", data=b"", headers=AUTH, timeout=10, verify=str(other_ca))


def test_server_certificate_names(collector):
    """Certificate must be valid for the names agents really use."""
    for host in ("localhost", "127.0.0.1"):
        url = f"https://{host}:{HTTP_PORT}/v1/traces"
        r = requests.post(url, data=b"", headers={"Content-Type": "application/x-protobuf", **AUTH}, timeout=10, verify=collector.ca)
        assert r.status_code == 200, host


def test_spans_are_delivered_over_tls(send, backend):
    send(lambda t: t.start_span("encrypted").end())
    backend.wait_for(lambda s: s["name"] == "encrypted")
