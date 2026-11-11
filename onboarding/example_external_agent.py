"""
A small SIMULATED agent from "another team", to show the onboarding path end to end.
It uses only onboarding/otel_setup.py (the copy-paste template) and a fake language model,
so it needs no GPU, no API key for a model and no other code from this repository.

Run (PowerShell):
  $env:OTEL_EXPORTER_OTLP_ENDPOINT = "https://localhost:4318"
  $env:AGENT_API_KEY = "<your team's key>"
  $env:AGENT_CA_CERT = "certs\\k8s-ca.crt"
  python onboarding/example_external_agent.py --question "Where is order 1042?"
Add --pii to include FAKE personal data, to see it masked in Langfuse.
"""
import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from otel_setup import (llm_call, record_llm_result, record_tool_result,  # noqa: E402
                        setup_tracing, tool_call, trace_run)

ORDERS = {"1042": "shipped on 2026-11-08, arrives 2026-11-12", "2077": "being packed", "3310": "delivered"}
PII_QUESTION = "I am jane.roe@example.com, phone +91 98765 43210. Where is order 1042?"


def lookup_order(order_id):
    if order_id not in ORDERS:
        raise KeyError(f"order {order_id} not found")
    return ORDERS[order_id]


def fake_llm(messages):
    """Stands in for a real model call so the demo is repeatable."""
    last = messages[-1]["content"]
    text = "Here is what I found: " + last.split("Tool result:", 1)[-1].strip()
    return text, sum(len(m["content"].split()) for m in messages), len(text.split())


def run(tracer, question):
    words = question.replace("?", " ").split()
    order_id = next((w for w in words if w.isdigit() and len(w) == 4), "")
    with trace_run(tracer, "order-support-agent", question, user_id="external-user-001",
                   session_id=f"session-{uuid.uuid4().hex[:8]}", tags=["onboarding-demo"]) as root:
        with tool_call(tracer, "order_lookup", {"order_id": order_id}) as tool:
            result = lookup_order(order_id)
            record_tool_result(tool, result)
        messages = [{"role": "system", "content": "Answer using the tool result."},
                    {"role": "user", "content": f"Question: {question}\nTool result: {result}"}]
        with llm_call(tracer, "llm.answer", "fake-llm", "demo", messages) as span:
            text, in_tokens, out_tokens = fake_llm(messages)
            record_llm_result(span, text, in_tokens, out_tokens)
        root.set_attribute("langfuse.trace.output", text)
        return text, format(root.get_span_context().trace_id, "032x")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="Where is order 1042?")
    parser.add_argument("--pii", action="store_true", help="send FAKE personal data to test masking")
    args = parser.parse_args()
    provider, tracer = setup_tracing("demo-support-agent", team="external-team-demo",
                                     platform="simulated-external")
    try:
        answer, trace_id = run(tracer, PII_QUESTION if args.pii else args.question)
    finally:
        provider.shutdown()
    print("Answer:", answer)
    print("Trace ID:", trace_id)


if __name__ == "__main__":
    main()
