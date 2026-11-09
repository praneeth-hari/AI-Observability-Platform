"""
Step 2 of the Enterprise AI Observability prototype: a REAL agent built with LangGraph.

Unlike agent.py (a fixed script: plan -> calculate -> answer), here the model DECIDES at run
time which tool to call, can call several tools in a row, and stops when it has enough:

        +--------+  tool chosen   +------+
  --->  |  plan  | -------------> | tool |
        +--------+ <------------- +------+
            |   (loop, max MAX_STEPS)
            | final
            v
        +--------+
        | answer | ---> END
        +--------+

Every model call and tool call is traced with OpenTelemetry and sent to the SAME collector
gateway as before (API key + TLS + PII redaction), so it shows up in Langfuse unchanged.

Tools:  calculator (safe math), lookup (small company knowledge base), clock (current UTC time)

Usage:
  python langgraph_agent.py                                   # default question
  python langgraph_agent.py --question "What time is it, and what is 12 * 7?"
  python langgraph_agent.py --mock                            # scripted mock LLM (no Ollama needed)
  python langgraph_agent.py --fail                            # calculator breaks; agent must cope
  python langgraph_agent.py --pii                             # FAKE sensitive data, tests redaction
"""
import argparse
import ast
import datetime
import json
import operator
import os
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

import agent as base  # the Step 1 agent: tracing setup, LLM backends, mock LLM

MAX_STEPS = 4            # safety limit: the planner can call at most this many tools
MAX_INPUT_CHARS = 200    # tool inputs longer than this are refused (guards against junk from the model)

PLANNER_SYSTEM = (
    "GRAPH_PLANNER. You are the planning step of an agent. Decide the next action.\n"
    "Tools:\n"
    '- calculator: do arithmetic. input = a math expression like "18% of 2450" or "(12+3)*7".\n'
    '- lookup: look up a company policy or fact. input = a short topic like "refund policy".\n'
    "- clock: get the current UTC date and time. input = empty string.\n"
    "Respond ONLY with JSON. To use a tool: "
    '{"action": "tool", "tool": "<tool name>", "input": "<tool input>"}. '
    'If you already have everything needed to answer: {"action": "final"}.\n'
    "Use at most one tool per reply. Do not repeat a tool call you already made."
)


# ---------------------------------------------------------------- tools
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("unsupported expression")


def tool_calculator(expression: str) -> str:
    """Safe arithmetic. Understands 'N% of M' and + - * / ** % with brackets. Never uses eval()."""
    expr = expression.strip().lower().replace(",", "").replace("x", "*").replace("×", "*")
    m = re.fullmatch(r"([\d.]+)\s*%\s*of\s*([\d.]+)", expr)
    try:
        value = float(m[1]) / 100 * float(m[2]) if m else _eval_node(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        raise ValueError("division by zero")
    except (SyntaxError, ValueError, TypeError, OverflowError) as e:
        raise ValueError(f"cannot calculate '{expression}': {e}")
    return str(round(value, 6))


KNOWLEDGE_BASE = {
    "refund policy": "Refunds are issued to the original payment method within 7 business days of approval.",
    "working hours": "Support is available Monday to Friday, 9:00 to 18:00 IST.",
    "gst rate": "The standard GST rate for software services is 18%.",
    "data retention": "Telemetry is retained for 30 days, then deleted automatically.",
    "escalation": "Unresolved P1 incidents escalate to the on-call engineering manager after 30 minutes.",
}


def tool_lookup(topic: str) -> str:
    """Find a fact in the small knowledge base by topic words."""
    words = set(re.findall(r"[a-z0-9]+", topic.lower()))
    best, best_score = None, 0
    for key, fact in KNOWLEDGE_BASE.items():
        score = len(words & set(key.split()))
        if score > best_score:
            best, best_score = fact, score
    if not best:
        raise LookupError(f"nothing found for '{topic}'. Known topics: {', '.join(KNOWLEDGE_BASE)}")
    return best


def tool_clock(_: str = "") -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


TOOLS: Dict[str, Callable[[str], str]] = {
    "calculator": tool_calculator,
    "lookup": tool_lookup,
    "clock": tool_clock,
}


# ---------------------------------------------------------------- graph state
class AgentState(TypedDict):
    question: str
    observations: List[Dict[str, str]]   # [{"tool":..., "input":..., "output":...}, ...]
    next: Dict[str, Any]                 # the planner's latest decision
    steps: int
    answer: str


def format_observations(obs: List[Dict[str, str]]) -> str:
    if not obs:
        return "(none)"
    return "\n".join(f'- {o["tool"]}("{o["input"]}") -> {o["output"]}' for o in obs)


def _json_objects(text: str):
    """Yield every JSON object found in the text, in order. Small models often add chatter,
    ``` fences, or write several objects in one reply; a greedy regex cannot handle that."""
    decoder, i = json.JSONDecoder(), 0
    while True:
        i = text.find("{", i)
        if i == -1:
            return
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end


def parse_decision(text: str) -> Dict[str, Any]:
    """Turn the model's reply into ONE decision. Takes the first valid tool request (the loop
    comes back for the others). Anything we cannot trust becomes 'final'."""
    saw_final = False
    for data in _json_objects(text or ""):
        tool = data.get("tool")
        if tool in TOOLS and data.get("action", "tool") == "tool":   # tolerate a missing "action"
            return {"action": "tool", "tool": tool, "input": str(data.get("input", ""))}
        saw_final = saw_final or data.get("action") == "final"
    return {"action": "final", "reason": "model chose to answer" if saw_final else "no valid plan"}


# ---------------------------------------------------------------- building the graph
def build_graph(tracer, backend: str, parent_ctx, fail: bool = False, pii: bool = False):
    """Create the LangGraph for ONE run. parent_ctx = the agent.run span, so every node's spans
    are children of it no matter which thread LangGraph executes the node on."""

    def in_run(fn):
        def wrapper(state):
            token = otel_context.attach(parent_ctx)
            try:
                return fn(state)
            finally:
                otel_context.detach(token)
        return wrapper

    def plan(state: AgentState):
        text = base.call_llm(tracer, "llm.planner", [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": f"Question: {state['question']}\n"
                                        f"Tool results so far:\n{format_observations(state['observations'])}"},
        ], backend)
        decision = parse_decision(text)
        # never let the model loop on the same call, and never exceed the step limit
        if decision["action"] == "tool":
            repeated = any(o["tool"] == decision["tool"] and o["input"] == decision["input"]
                           for o in state["observations"])
            if repeated or state["steps"] >= MAX_STEPS:
                decision = {"action": "final", "reason": "repeat or step limit"}
        return {"next": decision}

    def run_tool(state: AgentState):
        d = state["next"]
        name, tool_input = d["tool"], d["input"]
        with tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("langfuse.observation.type", "tool")
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("langfuse.observation.input", tool_input)
            try:
                if len(tool_input) > MAX_INPUT_CHARS:
                    raise ValueError("tool input too long")
                if fail and name == "calculator":
                    if pii:
                        raise ValueError("lookup failed for customer john.doe@example.com, "
                                         "card 4111 1111 1111 1111")
                    raise TimeoutError("calculator service timed out after 2000 ms")
                output = TOOLS[name](tool_input)
                span.set_attribute("langfuse.observation.output", output)
            except Exception as e:  # the agent keeps going: the error becomes an observation
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_attribute("langfuse.observation.status_message", str(e))
                output = f"ERROR: {type(e).__name__}"
        return {"observations": state["observations"] + [{"tool": name, "input": tool_input, "output": output}],
                "steps": state["steps"] + 1}

    def answer(state: AgentState):
        text = base.call_llm(tracer, "llm.answer", [
            {"role": "system", "content": "Answer the user in one short sentence using the tool results. "
                                          "If a tool failed, say what could not be done."},
            {"role": "user", "content": f"Question: {state['question']}\n"
                                        f"Tool results:\n{format_observations(state['observations'])}"},
        ], backend)
        return {"answer": text}

    def route(state: AgentState) -> str:
        return "tool" if state["next"]["action"] == "tool" else "answer"

    g = StateGraph(AgentState)
    g.add_node("plan", in_run(plan))
    g.add_node("tool", in_run(run_tool))
    g.add_node("answer", in_run(answer))
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", route, {"tool": "tool", "answer": "answer"})
    g.add_edge("tool", "plan")
    g.add_edge("answer", END)
    return g.compile()


# ---------------------------------------------------------------- one run
def run_agent(tracer, question: str, backend: str, fail: bool = False, pii: bool = False) -> str:
    """Run the graph once, fully traced. Returns the trace id."""
    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("langfuse.observation.type", "agent")
        root.set_attribute("langfuse.trace.name", "langgraph-assistant")
        root.set_attribute("langfuse.session.id", f"session-{uuid.uuid4().hex[:8]}")
        root.set_attribute("langfuse.user.id", "test-user-001")
        root.set_attribute("langfuse.trace.tags", ["prototype", "step-2", "langgraph"])
        root.set_attribute("agent.framework", "langgraph")
        if pii:  # fake secrets, to prove the collector's deny rules still drop them
            root.set_attribute("http.request.header.authorization", "Bearer abcdefghijklmnop1234567890")
            root.set_attribute("db.password", "not-a-real-password")
        root.set_attribute("langfuse.trace.input", question)
        root.set_attribute("langfuse.observation.input", question)
        trace_id = format(root.get_span_context().trace_id, "032x")

        try:
            graph = build_graph(tracer, backend, otel_context.get_current(), fail=fail, pii=pii)
            final = graph.invoke({"question": question, "observations": [], "next": {}, "steps": 0, "answer": ""})
            root.set_attribute("agent.steps", final["steps"])
            root.set_attribute("langfuse.trace.output", final["answer"])
            root.set_attribute("langfuse.observation.output", final["answer"])
            print(f"Answer: {final['answer']}")
        except Exception as e:  # e.g. the LLM itself is unreachable
            root.record_exception(e)
            root.set_status(Status(StatusCode.ERROR, str(e)))
            root.set_attribute("langfuse.observation.level", "ERROR")
            root.set_attribute("langfuse.trace.output", f"FAILED: {e}")
            print(f"Agent failed: {e}")
        return trace_id


def pick_backend(force_mock: bool) -> str:
    if force_mock:
        return "mock"
    if base.HF_TOKEN:
        return "hf"
    return "ollama" if base.ollama_available() else "mock"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--question", default="What is 18% of 2450?")
    p.add_argument("--mock", action="store_true", help="force the scripted mock LLM")
    p.add_argument("--fail", action="store_true", help="make the calculator tool fail")
    p.add_argument("--pii", action="store_true", help="send FAKE sensitive data to test redaction")
    args = p.parse_args()

    backend = pick_backend(args.mock)
    if not base.AGENT_API_KEY:
        print("WARNING: AGENT_API_KEY is not set (.env) - the collector will reject this agent's traces.")
    model = {"hf": base.HF_MODEL, "ollama": base.OLLAMA_MODEL, "mock": "mock"}[backend]
    print(f"LLM backend: {backend} ({model})")

    provider, tracer = base.setup_tracer()
    tid = run_agent(tracer, base.PII_QUESTION if args.pii else args.question, backend, args.fail, args.pii)
    provider.force_flush()
    provider.shutdown()
    print(f"Trace ID: {tid}")
    print("Search this ID in Langfuse -> http://localhost:3000 (Tracing)")
