"""
Starter code for sending YOUR AI agent's traces to the AI Observability platform.

How to use: copy this one file into your agent project (it does not depend on anything else in this
repository), then see onboarding/example_external_agent.py and docs/ONBOARDING.md.

It needs only: opentelemetry-sdk and opentelemetry-exporter-otlp-proto-http (see requirements.txt).

Settings are read from environment variables, never from code:
  OTEL_EXPORTER_OTLP_ENDPOINT   address of the collector, e.g. https://collector.example.com:4318
  AGENT_API_KEY                 the key the platform team gave your team
  AGENT_CA_CERT                 (optional) path to the CA certificate file, if the collector uses a private CA
"""
import contextlib
import json
import os

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode


class ConfigError(RuntimeError):
    """A required setting is missing or unsafe."""


def setup_tracing(service_name, team, platform="python", version="0.1.0", span_processor=None):
    """Create the tracer. Returns (provider, tracer).

    Call provider.shutdown() before your program ends, otherwise the last spans may not be sent.
    `span_processor` is only for tests; normally leave it out.
    """
    resource = Resource.create({
        "service.name": service_name,      # name of your agent
        "service.version": version,
        "agent.platform": platform,        # where it runs, e.g. "databricks", "aws-lambda", "laptop"
        "team": team,                      # your team's name, so traces can be filtered per team
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(span_processor or _collector_processor())
    return provider, provider.get_tracer(service_name)


def _collector_processor():
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    key = os.getenv("AGENT_API_KEY", "")
    missing = [n for n, v in (("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint), ("AGENT_API_KEY", key)) if not v]
    if missing:
        raise ConfigError("Missing setting(s): " + ", ".join(missing) + ". See docs/ONBOARDING.md, step 2.")
    if not endpoint.startswith("https://"):
        raise ConfigError("OTEL_EXPORTER_OTLP_ENDPOINT must start with https:// (the API key must never travel unencrypted).")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    ca = os.getenv("AGENT_CA_CERT") or None
    return BatchSpanProcessor(OTLPSpanExporter(
        endpoint=endpoint.rstrip("/") + "/v1/traces",
        headers={"Authorization": f"Bearer {key}"},
        certificate_file=ca,
    ))


def _fail(span, error):
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, str(error)))
    span.set_attribute("langfuse.observation.level", "ERROR")
    span.set_attribute("langfuse.observation.status_message", str(error))


@contextlib.contextmanager
def trace_run(tracer, name, user_input, user_id=None, session_id=None, tags=None):
    """One agent run = one trace. Wrap your whole agent call in this."""
    with tracer.start_as_current_span(name) as root:
        root.set_attribute("langfuse.trace.name", name)
        root.set_attribute("langfuse.trace.input", user_input)
        if user_id:
            root.set_attribute("langfuse.user.id", user_id)
        if session_id:
            root.set_attribute("langfuse.session.id", session_id)
        if tags:
            root.set_attribute("langfuse.trace.tags", list(tags))
        try:
            yield root
        except Exception as error:
            root.set_attribute("langfuse.trace.output", f"FAILED: {error}")
            _fail(root, error)
            raise


@contextlib.contextmanager
def llm_call(tracer, name, model, provider, messages):
    """Wrap each call to a language model. Then call record_llm_result() with the answer."""
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("langfuse.observation.input", json.dumps(messages))
        try:
            yield span
        except Exception as error:
            _fail(span, error)
            raise


def record_llm_result(span, text, input_tokens, output_tokens):
    """Attach the model's answer and token counts (these feed the cost dashboards)."""
    span.set_attribute("langfuse.observation.output", text)
    span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))


@contextlib.contextmanager
def tool_call(tracer, name, tool_input):
    """Wrap each tool your agent uses (search, database lookup, calculator...)."""
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("langfuse.observation.type", "tool")
        span.set_attribute("langfuse.observation.input", str(tool_input))
        try:
            yield span
        except Exception as error:
            _fail(span, error)
            raise


def record_tool_result(span, result):
    span.set_attribute("langfuse.observation.output", str(result))
