"""
Sample instrumented AI agent - Step 1 of the Enterprise AI Observability prototype.

Simulates one agent run:  agent-run -> planner (LLM) -> tool call -> answer (LLM)
and exports the spans over OTLP/HTTP to the collector gateway (not directly to Langfuse).

LLM backends (picked automatically, in this order):
  1. Hugging Face hosted API - if HF_TOKEN is set (in the environment or the project's .env file)
  2. Local Ollama             - if Ollama is running
  3. Mock LLM                 - so the trace path can always be tested

Usage:
  python agent.py                         # one normal run
  python agent.py --question "What is 18% of 2450?"
  python agent.py --fail                  # tool fails -> error span, for debugging demos
  python agent.py --mock                  # force the mock LLM
  python agent.py --pii                   # question contains FAKE sensitive data (tests redaction)
  python agent.py --pii --fail            # same, and the error message also contains fake PII
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


def load_dotenv(path):
    """Tiny .env reader (no extra package). Existing environment variables win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.split(" #", 1)[0].strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), value)
    except OSError:
        pass


# The project's .env sits one folder above this file
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

COLLECTOR = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://localhost:4318")
# CA certificate the agent trusts for the collector's TLS (created by scripts/make_certs.py)
CA_CERT = os.getenv("AGENT_CA_CERT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "certs", "ca.crt"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")   # must match the collector's key
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-7B-Instruct")
HF_URL = os.getenv("HF_URL", "https://router.huggingface.co/v1/chat/completions")


# FAKE sensitive data used only by --pii to test the collector's redaction
PII_QUESTION = (
    "My email is john.doe@example.com, phone +91 98765 43210, card 4111 1111 1111 1111, "
    "SSN 123-45-6789, MRN: 00481516. What is 18% of 2450?"
)


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
        BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{COLLECTOR}/v1/traces",
            headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
            certificate_file=CA_CERT if COLLECTOR.startswith("https://") else None,
        ))
    )
    trace.set_tracer_provider(provider)
    return provider, trace.get_tracer("ai-obs.sample-agent")


# ---------- LLM call (Hugging Face, Ollama or mock) ----------
def call_llm(tracer, name, messages, backend):
    model = {"hf": HF_MODEL, "ollama": OLLAMA_MODEL, "mock": "mock-llm"}[backend]
    system = {"hf": "huggingface", "ollama": "ollama", "mock": "mock"}[backend]
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", system)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.request.temperature", 0.0)
        span.set_attribute("langfuse.observation.input", json.dumps(messages))

        try:
            if backend == "mock":
                time.sleep(0.3)
                text = mock_reply(messages)
                in_tok, out_tok = sum(len(m["content"].split()) for m in messages), len(text.split())
            elif backend == "hf":
                r = requests.post(
                    HF_URL,
                    headers={"Authorization": f"Bearer {HF_TOKEN}"},
                    json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 256},
                    timeout=120,
                )
                if not r.ok:
                    # Never include the token; show only the status and the API's message
                    raise RuntimeError(f"Hugging Face API error {r.status_code}: {r.text[:300]}")
                data = r.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            else:
                r = requests.post(f"{OLLAMA_URL}/api/chat", json={
                    "model": model, "messages": messages, "stream": False,
                    "options": {"temperature": 0},
                }, timeout=120)
                r.raise_for_status()
                data = r.json()
                text = data["message"]["content"]
                in_tok, out_tok = data.get("prompt_eval_count", 0), data.get("eval_count", 0)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", str(e))
            raise

        span.set_attribute("langfuse.observation.output", text)
        span.set_attribute("gen_ai.usage.input_tokens", in_tok)
        span.set_attribute("gen_ai.usage.output_tokens", out_tok)
        return text


def mock_graph_planner(last):
    """Scripted planner for langgraph_agent.py: uses each tool the question needs, then finishes."""
    question = last.split("Question:", 1)[-1].split("\nTool results so far:")[0].strip()
    used = set(re.findall(r"^- (\w+)\(", last.split("Tool results so far:", 1)[-1], re.M))
    q = question.lower()
    needed = []
    if re.search(r"\btime\b|\bdate\b|\bclock\b|\btoday\b", q):
        needed.append(("clock", ""))
    for topic in ("refund policy", "working hours", "gst rate", "data retention", "escalation"):
        if all(w in q for w in topic.split()[:1]):
            needed.append(("lookup", topic))
            break
    expr = re.findall(r"\d+(?:\.\d+)?\s*%\s*of\s*[\d.]+", question) or \
        re.findall(r"\(?\d+(?:\.\d+)?\s*[*/+-]\s*\d+(?:\.\d+)?\)?", question)
    if expr:
        needed.append(("calculator", expr[0]))
    for tool, tool_input in needed:
        if tool not in used:
            return json.dumps({"action": "tool", "tool": tool, "input": tool_input})
    return json.dumps({"action": "final"})


def mock_reply(messages):
    last = messages[-1]["content"]
    if "GRAPH_PLANNER" in messages[0]["content"]:
        return mock_graph_planner(last)
    if "Tool results:" in last and "Question:" in last and "Tool result:" not in last:
        results = last.split("Tool results:", 1)[-1].strip()
        if "ERROR" in results:
            return "I could not complete that: a tool failed."
        return "Based on the tools: " + "; ".join(l.split("->", 1)[-1].strip().rstrip(".") for l in results.splitlines()) + "."
    if "Respond ONLY with JSON" in messages[0]["content"]:
        expr = re.findall(r"\d+(?:\.\d+)?\s*%\s*of\s*[\d.]+", last) or \
               re.findall(r"\d+(?:\.\d+)?\s*[*/+-]\s*\d+(?:\.\d+)?", last)
        return json.dumps({"tool": "calculator", "input": expr[0] if expr else "18% of 2450"})
    return f"Based on the tool result, the answer is {last.split('Tool result:')[-1].strip()}."


# ---------- Tool ----------
def calculator(tracer, expression, fail=False, pii=False):
    with tracer.start_as_current_span("tool.calculator") as span:
        span.set_attribute("langfuse.observation.type", "tool")
        span.set_attribute("gen_ai.tool.name", "calculator")
        span.set_attribute("langfuse.observation.input", expression)
        time.sleep(0.1)
        try:
            if fail:
                if pii:
                    raise ValueError("lookup failed for customer john.doe@example.com, card 4111 1111 1111 1111")
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
def run_agent(tracer, question, backend, fail, pii=False):
    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("langfuse.observation.type", "agent")
        root.set_attribute("langfuse.trace.name", "finance-assistant")
        root.set_attribute("langfuse.session.id", f"session-{uuid.uuid4().hex[:8]}")
        root.set_attribute("langfuse.user.id", "test-user-001")
        root.set_attribute("langfuse.trace.tags", ["prototype", "step-1"])
        if pii:  # fake secrets, to prove the deny rules drop them
            root.set_attribute("http.request.header.authorization", "Bearer abcdefghijklmnop1234567890")
            root.set_attribute("db.password", "not-a-real-password")
        root.set_attribute("langfuse.trace.input", question)
        root.set_attribute("langfuse.observation.input", question)
        trace_id = format(root.get_span_context().trace_id, "032x")

        try:
            plan = call_llm(tracer, "llm.planner", [
                {"role": "system", "content": 'You are a planner. Respond ONLY with JSON like '
                                              '{"tool": "calculator", "input": "<math expression>"}.'},
                {"role": "user", "content": question},
            ], backend)
            try:
                step = json.loads(re.search(r"\{.*\}", plan, re.S).group(0))
            except Exception:
                step = {"tool": "calculator", "input": question}

            result = calculator(tracer, step["input"], fail=fail, pii=pii)

            answer = call_llm(tracer, "llm.answer", [
                {"role": "system", "content": "Answer the user in one short sentence."},
                {"role": "user", "content": f"Question: {question}\nTool result: {result}"},
            ], backend)
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
    p.add_argument("--mock", action="store_true", help="force the mock LLM")
    p.add_argument("--fail", action="store_true", help="force the tool to fail")
    p.add_argument("--pii", action="store_true", help="send FAKE sensitive data to test redaction")
    args = p.parse_args()

    if args.mock:
        backend = "mock"
    elif HF_TOKEN:
        backend = "hf"
    elif ollama_available():
        backend = "ollama"
    else:
        backend = "mock"
    if not AGENT_API_KEY:
        print("WARNING: AGENT_API_KEY is not set (.env) - the collector will reject this agent's traces.")
    print(f"LLM backend: {backend}" + (f" ({HF_MODEL})" if backend == "hf" else ""))

    provider, tracer = setup_tracer()
    question = PII_QUESTION if args.pii else args.question
    tid = run_agent(tracer, question, backend, args.fail, pii=args.pii)
    provider.force_flush()
    provider.shutdown()
    print(f"Trace ID: {tid}")
    print(f"Search this ID in Langfuse -> http://localhost:3000 (Tracing)")
