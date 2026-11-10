"""Tests for the Step 2 LangGraph agent: tools, graph behaviour, and that its traces flow through
the REAL collector (auth + TLS + redaction) in the shape Langfuse expects. Uses the scripted mock LLM,
so no Ollama or internet is needed."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import agent as base  # noqa: E402
import langgraph_agent as la  # noqa: E402


# ---------------- tools ----------------
@pytest.mark.parametrize("expr,expected", [
    ("18% of 2450", "441.0"), ("(12+3)*7", "105"), ("10/4", "2.5"), ("2**10", "1024"), ("-5+8", "3"),
    ("1,000 + 1", "1001"), ("6 x 7", "42"),
])
def test_calculator_correct(expr, expected):
    assert la.tool_calculator(expr) == expected


@pytest.mark.parametrize("expr", ["__import__('os').system('x')", "open('f')", "2**9999", "1/0", "abc", ""])
def test_calculator_refuses_unsafe_or_invalid(expr):
    with pytest.raises(ValueError):
        la.tool_calculator(expr)


def test_lookup_finds_and_misses():
    assert "7 business days" in la.tool_lookup("what is your refund policy?")
    with pytest.raises(LookupError):
        la.tool_lookup("quantum cheese")


def test_clock_format():
    assert la.tool_clock("").endswith("UTC")


# ---------------- decision parsing ----------------
def test_parse_decision_valid_tool():
    d = la.parse_decision('Sure! {"action": "tool", "tool": "calculator", "input": "2+2"}')
    assert d == {"action": "tool", "tool": "calculator", "input": "2+2"}


def test_parse_decision_two_objects_takes_the_first():
    """Real case seen with llama3.1/qwen: two JSON objects in one reply."""
    d = la.parse_decision('{"action": "tool", "tool": "clock", "input": ""}\n'
                          '{"action": "tool", "tool": "calculator", "input": "12*7"}')
    assert d == {"action": "tool", "tool": "clock", "input": ""}


@pytest.mark.parametrize("text", [
    'Sure:\n```json\n{"action": "tool", "tool": "calculator", "input": "2+2"}\n```',
    '{"tool": "calculator", "input": "2+2"}',                      # model forgot "action"
    '{"oops": 1} then {"action": "tool", "tool": "calculator", "input": "2+2"}',
])
def test_parse_decision_tolerates_messy_model_output(text):
    d = la.parse_decision(text)
    assert d["action"] == "tool" and d["tool"] == "calculator" and d["input"] == "2+2"


def test_parse_decision_unknown_tool_then_valid_tool_uses_valid_one():
    d = la.parse_decision('{"action": "tool", "tool": "rm_rf", "input": "/"} {"action": "tool", "tool": "clock", "input": ""}')
    assert d["tool"] == "clock"


@pytest.mark.parametrize("text", ["no json here", "{broken", '{"action": "tool", "tool": "rm_rf", "input": "/"}',
                                  '{"action": "dance"}', ""])
def test_parse_decision_untrusted_becomes_final(text):
    assert la.parse_decision(text)["action"] == "final"


# ---------------- graph + traces through the real collector ----------------
def run(send, backend, question, **kw):
    out = {}
    send(lambda tracer: out.update(tid=la.run_agent(tracer, question, "mock", **kw)))
    return out["tid"]


def trace_spans(backend, tid):
    backend.wait_for(lambda s: s["trace_id"] == tid and s["name"] == "agent.run")
    return [s for s in backend.spans if s["trace_id"] == tid]


def names(spans):
    return sorted(s["name"] for s in spans)


def test_one_tool_run_shape(send, backend):
    """plan -> tool -> plan -> answer"""
    spans = trace_spans(backend, run(send, backend, "What is 18% of 2450?"))
    assert names(spans) == ["agent.run", "llm.answer", "llm.planner", "llm.planner", "tool.calculator"]
    root = next(s for s in spans if s["name"] == "agent.run")
    assert root["parent_id"] == ""
    assert root["attrs"]["agent.framework"] == "langgraph"
    assert root["attrs"]["agent.steps"] == 1
    assert "441" in root["attrs"]["langfuse.trace.output"]


def test_every_span_is_a_child_of_agent_run(send, backend):
    """Guards against LangGraph running nodes on another thread and orphaning the spans."""
    spans = trace_spans(backend, run(send, backend, "What time is it, and what is 12 * 7?"))
    root = next(s for s in spans if s["name"] == "agent.run")
    for s in spans:
        if s is not root:
            assert s["parent_id"] == root["span_id"], s["name"]


def test_agent_uses_several_tools_in_order(send, backend):
    spans = trace_spans(backend, run(send, backend, "What time is it, and what is 12 * 7?"))
    tools = [s["name"] for s in sorted(spans, key=lambda s: s["span_id"]) if s["name"].startswith("tool.")]
    assert sorted(tools) == ["tool.calculator", "tool.clock"]
    assert names(spans).count("llm.planner") == 3          # two tool decisions + the final decision
    root = next(s for s in spans if s["name"] == "agent.run")
    assert root["attrs"]["agent.steps"] == 2
    assert "84" in root["attrs"]["langfuse.trace.output"]


def test_question_needing_no_tool_goes_straight_to_answer(send, backend):
    spans = trace_spans(backend, run(send, backend, "Say hello"))
    assert names(spans) == ["agent.run", "llm.answer", "llm.planner"]


def test_langfuse_attributes_present(send, backend):
    spans = trace_spans(backend, run(send, backend, "What is the refund policy?"))
    kinds = {s["name"]: s["attrs"].get("langfuse.observation.type") for s in spans}
    assert kinds["agent.run"] == "agent" and kinds["llm.planner"] == "generation"
    assert kinds["llm.answer"] == "generation" and kinds["tool.lookup"] == "tool"
    gen = next(s for s in spans if s["name"] == "llm.answer")
    assert gen["attrs"]["gen_ai.request.model"] == "mock-llm"
    assert gen["attrs"]["gen_ai.usage.input_tokens"] > 0


def test_tool_failure_is_recorded_and_agent_recovers(send, backend):
    spans = trace_spans(backend, run(send, backend, "What is 18% of 2450?", fail=True))
    tool = next(s for s in spans if s["name"] == "tool.calculator")
    assert tool["attrs"]["langfuse.observation.level"] == "ERROR"
    assert "timed out" in tool["attrs"]["langfuse.observation.status_message"]
    root = next(s for s in spans if s["name"] == "agent.run")
    assert "could not" in root["attrs"]["langfuse.trace.output"].lower()   # the run still finished
    assert "llm.answer" in names(spans)


def test_pii_is_redacted_for_langgraph_traces_too(send, backend):
    tid = run(send, backend, base.PII_QUESTION, fail=True, pii=True)
    trace_spans(backend, tid)
    backend.wait_for(lambda s: s["name"] == "llm.answer")
    leaked = backend.everything()
    for secret in ["john.doe@example.com", "4111 1111 1111 1111", "123-45-6789", "98765 43210",
                   "abcdefghijklmnop1234567890", "not-a-real-password"]:
        assert secret not in leaked, secret
    assert "[REDACTED_EMAIL]" in leaked


def test_planner_loop_is_bounded(monkeypatch):
    """A model that asks for a new tool call forever must still stop at MAX_STEPS."""
    counter = {"n": 0}

    def endless(tracer, name, messages, backend):
        if name == "llm.planner":
            counter["n"] += 1
            return '{"action": "tool", "tool": "calculator", "input": "%d+1"}' % counter["n"]
        return "done"

    monkeypatch.setattr(base, "call_llm", endless)
    from opentelemetry.sdk.trace import TracerProvider
    tracer = TracerProvider().get_tracer("t")
    from opentelemetry import context
    graph = la.build_graph(tracer, "mock", context.get_current())
    final = graph.invoke({"question": "x", "observations": [], "next": {}, "steps": 0, "answer": ""})
    assert final["steps"] == la.MAX_STEPS and final["answer"] == "done"


def test_repeated_identical_tool_call_is_stopped(monkeypatch):
    monkeypatch.setattr(base, "call_llm", lambda t, name, m, b: (
        '{"action": "tool", "tool": "clock", "input": ""}' if name == "llm.planner" else "ok"))
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry import context
    graph = la.build_graph(TracerProvider().get_tracer("t"), "mock", context.get_current())
    final = graph.invoke({"question": "x", "observations": [], "next": {}, "steps": 0, "answer": ""})
    assert final["steps"] == 1
