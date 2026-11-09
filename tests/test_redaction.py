"""Privacy tests: sensitive data must never reach the backend (Langfuse)."""
import json

import pytest
from opentelemetry.trace import Status, StatusCode

# (id, text sent, the secret part that must NOT appear afterwards, label that SHOULD appear)
CASES = [
    ("email", "contact john.doe@example.com today", "john.doe@example.com", "[REDACTED_EMAIL]"),
    ("email_plus", "mail a.b+tag@sub.domain.co.in", "a.b+tag@sub.domain.co.in", "[REDACTED_EMAIL]"),
    ("phone_india_plus91", "call +91 98765 43210 now", "98765 43210", "[REDACTED_PHONE]"),
    ("phone_india_plain", "call 9876543210 now", "9876543210", "[REDACTED_PHONE]"),
    ("phone_us_dashes", "call 415-555-2671 now", "415-555-2671", "[REDACTED_PHONE]"),
    ("phone_us_brackets", "call (415) 555-2671 now", "555-2671", "[REDACTED_PHONE]"),
    ("card_spaces", "card 4111 1111 1111 1111 ok", "4111 1111 1111 1111", "[REDACTED_CARD]"),
    ("card_dashes", "card 4111-1111-1111-1111 ok", "4111-1111-1111-1111", "[REDACTED_CARD]"),
    ("card_plain", "card 4111111111111111 ok", "4111111111111111", "[REDACTED_CARD]"),
    ("ssn", "ssn 123-45-6789 ok", "123-45-6789", "[REDACTED_SSN]"),
    ("aadhaar", "id 1234 5678 9012 ok", "1234 5678 9012", "[REDACTED_ID]"),
    ("mrn", "patient MRN: 00481516 admitted", "00481516", "[REDACTED_MRN]"),
    ("bearer", "Authorization: Bearer abcdefghijklmnop1234567890", "abcdefghijklmnop1234567890", "[REDACTED_TOKEN]"),
    ("api_key", "key sk-abcdefghijklmnop1234 end", "sk-abcdefghijklmnop1234", "[REDACTED_KEY]"),
    ("hf_token", "token hf_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "A1b2C3d4E5f6G7h8I9j0K1l2", "[REDACTED_KEY]"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_value_is_masked(send, backend, case):
    _, text, secret, label = case
    send(lambda t: _span(t, "masked", {"langfuse.observation.input": text}))
    span = backend.wait_for(lambda s: s["name"] == "masked")[0]
    assert secret not in backend.everything(), f"LEAK: {secret!r} reached the backend"
    assert label in span["attrs"]["langfuse.observation.input"]


def _span(tracer, name, attrs):
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            sp.set_attribute(k, v)


def test_nested_json_payload_is_masked(send, backend):
    """Prompts are JSON strings; PII buried inside must still be caught."""
    payload = json.dumps([{"role": "user", "content": "I am john.doe@example.com, card 4111 1111 1111 1111"}])
    send(lambda t: _span(t, "nested", {"langfuse.observation.input": payload}))
    backend.wait_for(lambda s: s["name"] == "nested")
    everything = backend.everything()
    assert "john.doe" not in everything and "4111" not in everything


def test_secret_looking_attribute_names_are_dropped(send, backend):
    attrs = {"http.request.header.authorization": "Bearer abcdefghijklmnop1234567890",
             "db.password": "hunter2", "api_key": "xyz", "session.cookie": "abc", "safe.attr": "keep-me"}
    send(lambda t: _span(t, "keys", attrs))
    span = backend.wait_for(lambda s: s["name"] == "keys")[0]
    assert set(span["attrs"]) == {"safe.attr"}, f"unexpected attributes kept: {set(span['attrs'])}"
    assert "hunter2" not in backend.everything()


def test_error_status_and_exception_events_are_masked(send, backend):
    def fn(tracer):
        with tracer.start_as_current_span("boom") as sp:
            try:
                raise ValueError("lookup failed for john.doe@example.com card 4111 1111 1111 1111")
            except ValueError as e:
                sp.record_exception(e)
                sp.set_status(Status(StatusCode.ERROR, str(e)))
    send(fn)
    span = backend.wait_for(lambda s: s["name"] == "boom")[0]
    assert "[REDACTED_EMAIL]" in span["status_message"]
    assert "[REDACTED_CARD]" in span["status_message"]
    everything = backend.everything()
    assert "john.doe" not in everything and "4111" not in everything  # incl. stack trace


def test_resource_attributes_are_masked(send, backend):
    send(lambda t: _span(t, "res", {"x": "y"}),
         resource_attrs={"owner.contact": "jane@corp.example.org", "deploy.password": "pw123"})
    span = backend.wait_for(lambda s: s["name"] == "res")[0]
    assert span["resource"]["owner.contact"] == "[REDACTED_EMAIL]"
    assert "deploy.password" not in span["resource"]
    assert "jane@corp" not in backend.everything()


def test_normal_text_is_not_damaged(send, backend):
    """Redaction must not eat harmless content (false positives)."""
    attrs = {"langfuse.observation.input": "What is 18% of 2450? Order #12345 shipped on 2026-11-09 at 17:43:10.",
             "langfuse.observation.output": "Based on the tool result, the answer is 441.0.",
             "gen_ai.request.model": "Qwen/Qwen2.5-7B-Instruct"}
    send(lambda t: _span(t, "clean", attrs))
    span = backend.wait_for(lambda s: s["name"] == "clean")[0]
    for k, v in attrs.items():
        assert span["attrs"][k] == v, f"{k} was altered: {span['attrs'][k]!r}"


def test_oversized_attribute_is_truncated(send, backend):
    send(lambda t: _span(t, "big", {"langfuse.observation.input": "a" * 20000}))
    span = backend.wait_for(lambda s: s["name"] == "big")[0]
    assert len(span["attrs"]["langfuse.observation.input"]) <= 8000
