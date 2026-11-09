"""
Sample instrumented AI agent - Step 1 of the Enterprise AI Observability prototype.

Simulates one agent run:  agent-run -> planner (LLM) -> tool call -> answer (LLM)
and exports the spans over OTLP/HTTP to the collector gateway (not directly to Langfuse).

LLM: a local Ollama model (open source). If Ollama isn't running, it falls back to a
mock LLM so the trace path can still be tested.

Usage:
  python agent.py                         # one normal run
  python agent.py --question "What is 18% of 2450?"
  python agent.py --fail                  # tool fails -> error span, for debugging demos
  python agent.py --mock                  # skip Ollama
"""
import argparse
import json
import os
import re
import time
import uuid

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

COLLECTOR = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")


# ---------- OpenTelemetry setup ----------
def setup_tracer():
    resource = Resource.create({
        "service.name": "sample-finance-agent",
        "service.version": "0.1.0",
        "agent.platform": "local-python",   # later: "databricks"
        "team": "prototype",
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{COLLECTOR}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    return provider, trace.get_tracer("ai-obs.sample-agent")


# ---------- LLM call (Ollama or mock) ----------
def call_llm(tracer, name, messages, use_mock):
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "mock" if use_mock else "ollama")
        span.set_attribute("gen_ai.request.model", "mock-llm" if use_mock else MODEL)
        span.set_attribute("gen_ai.request.temperature", 0.0)
        span.set_attribute("langfuse.observation.input", json.dumps(messages))

        if use_mock:
            time.sleep(0.3)
            text = mock_reply(messages)
            in_tok, out_tok = sum(len(m["content"].split()) for m in messages), len(text.split())
        else:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json={
                "model": MODEL, "messages": messages, "stream": False,
                "options": {"temperature": 0},
            }, timeout=120)
            r.raise_for_status()
            data = r.json()
            text = data["message"]["content"]
            in_tok, out_tok = data.get("prompt_eval_count", 0), data.get("eval_count", 0)

        span.set_attribute("langfuse.observation.output", text)
        span.set_attribute("gen_ai.usage.input_tokens", in_tok)
        span.set_attribute("gen_ai.usage.output_tokens", out_tok)
        return text


def mock_reply(messages):
    last = messages[-1]["content"]
    if "Respond ONLY with JSON" in messages[0]["content"]:
        expr = re.findall(r"[\d.]+\s*[%*/+-]\s*(?:of\s*)?[\d.]+", last)
        return json.dumps({"tool": "calculator", "input": expr[0] if expr else "18% of 2450"})
    return f"Based on the tool result, the answer is {last.split('Tool result:')[-1].strip()}."


# ---------- Tool ----------
def calculator(tracer, expression, fail=False):
    with tracer.start_as_current_span("tool.calculator") as span:
        span.set_attribute("langfuse.observation.type", "tool")
        span.set_attribute("gen_ai.tool.name", "calculator")
        span.set_attribute("langfuse.observation.input", expression)
        time.sleep(0.1)
        try:
            if fail:
                raise TimeoutError("calculator service timed out after 2000 ms")
            expr = expression.replace(" ", "")
            m = re.match(r"([\d.]+)%of([\d.]+)", expr)
            result = float(m[1]) / 100 * float(m[2]) if m else eval(expr, {"__builtins__": {}})
            span.set_attribute("langfuse.observation.output", str(result))
            return str(result)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", str(e))
            raise


# ---------- Agent run ----------
def run_agent(tracer, question, use_mock, fail):
    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("langfuse.observation.type", "agent")
        root.set_attribute("langfuse.trace.name", "finance-assistant")
        root.set_attribute("langfuse.session.id", f"session-{uuid.uuid4().hex[:8]}")
        root.set_attribute("langfuse.user.id", "test-user-001")
        root.set_attribute("langfuse.trace.tags", ["prototype", "step-1"])
        root.set_attribute("langfuse.trace.input", question)
        root.set_attribute("langfuse.observation.input", question)
        trace_id = format(root.get_span_context().trace_id, "032x")

        try:
            plan = call_llm(tracer, "llm.planner", [
                {"role": "system", "content": 'You are a planner. Respond ONLY with JSON like '
                                              '{"tool": "calculator", "input": "<math expression>"}.'},
                {"role": "user", "content": question},
            ], use_mock)
            try:
                step = json.loads(re.search(r"\{.*\}", plan, re.S).group(0))
            except Exception:
                step = {"tool": "calculator", "input": question}

            result = calculator(tracer, step["input"], fail=fail)

            answer = call_llm(tracer, "llm.answer", [
                {"role": "system", "content": "Answer the user in one short sentence."},
                {"role": "user", "content": f"Question: {question}\nTool result: {result}"},
            ], use_mock)
            root.set_attribute("langfuse.trace.output", answer)
            root.set_attribute("langfuse.observation.output", answer)
            print(f"Answer: {answer}")
        except Exception as e:
            root.record_exception(e)
            root.set_status(Status(StatusCode.ERROR, str(e)))
            root.set_attribute("langfuse.observation.level", "ERROR")
            root.set_attribute("langfuse.trace.output", f"FAILED: {e}")
            print(f"Agent failed: {e}")
        return trace_id


def ollama_available():
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=2).ok
    except requests.RequestException:
        return False


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--question", default="What is 18% of 2450?")
    p.add_argument("--mock", action="store_true", help="use mock LLM instead of Ollama")
    p.add_argument("--fail", action="store_true", help="force the tool to fail")
    args = p.parse_args()

    use_mock = args.mock or not ollama_available()
    if use_mock and not args.mock:
        print(f"Ollama not reachable at {OLLAMA_URL} - using mock LLM.")

    provider, tracer = setup_tracer()
    tid = run_agent(tracer, args.question, use_mock, args.fail)
    provider.force_flush()
    provider.shutdown()
    print(f"Trace ID: {tid}")
    print(f"Search this ID in Langfuse -> http://localhost:3000 (Tracing)")
