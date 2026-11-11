# Onboarding: send your AI agent's traces to the platform

This guide is for a team that runs an AI agent and wants to see what it does: each step, its timing,
its token use and any errors. It takes about 30 minutes. You do not need to change how your agent works;
you add a few lines around it.

## How it works

```
your agent  --(HTTPS + API key)-->  collector  --(hides personal data)-->  Langfuse (the screens you look at)
```

The collector is the single front door. It checks your API key, masks personal data (names of people are not
masked, but e-mail addresses, phone numbers, card numbers and similar are) and then forwards the trace to Langfuse.

## What you need before you start

Ask the platform team for these three things:

| Item | What it is | Example |
|---|---|---|
| Collector address | Where to send traces | `https://collector.example.com:4318` |
| API key | Proves your agent is allowed to send | a long random string, keep it secret |
| CA certificate file | Lets your agent trust the collector's encryption (only needed when the platform uses a private certificate authority) | `k8s-ca.crt` |

Your agent's computer must be able to reach the collector address on port 4318.

## Step 1. Install two packages

```
pip install -r onboarding/requirements.txt
```

(That is `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http`.)

## Step 2. Set three environment variables

Never put the key in your code or in Git. Set it in your environment or secret store.

PowerShell:
```
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "https://collector.example.com:4318"
$env:AGENT_API_KEY = "<your key>"
$env:AGENT_CA_CERT = "C:\path\to\k8s-ca.crt"
```

Linux / macOS:
```
export OTEL_EXPORTER_OTLP_ENDPOINT="https://collector.example.com:4318"
export AGENT_API_KEY="<your key>"
export AGENT_CA_CERT="/path/to/k8s-ca.crt"
```

## Step 3. Copy the template and wrap your agent

Copy `onboarding/otel_setup.py` into your project. Then:

```python
from otel_setup import setup_tracing, trace_run, llm_call, record_llm_result, tool_call, record_tool_result

provider, tracer = setup_tracing("my-agent", team="my-team", platform="databricks")  # or "laptop", "aws"...

with trace_run(tracer, "my-agent-run", user_input=question, user_id="user-123") as root:   # one run = one trace
    with tool_call(tracer, "search_docs", {"query": question}) as tool:
        result = search_docs(question)
        record_tool_result(tool, result)

    with llm_call(tracer, "llm.answer", model="my-model", provider="my-provider", messages=messages) as span:
        text, in_tokens, out_tokens = call_my_model(messages)
        record_llm_result(span, text, in_tokens, out_tokens)

    root.set_attribute("langfuse.trace.output", text)

provider.shutdown()   # IMPORTANT: sends the last spans before the program ends
```

Three rules:
1. One `trace_run` around each agent run.
2. One `llm_call` around each model call, and always call `record_llm_result` with the token counts (they feed the cost dashboards).
3. Call `provider.shutdown()` before your program ends. For a long-running service, call it once when the service stops.

## Step 4. See your first trace

Run your agent once, wait about 30 seconds, then open Langfuse, go to **Tracing**, and look for your run.
Filter by your team name, or search by the Trace ID. You should see the run with its tool and model steps,
timings and token counts.

You can try the whole path first with the demo agent, which needs no model:

```
python onboarding/example_external_agent.py --question "Where is order 1042?"
python onboarding/example_external_agent.py --pii        # sends FAKE personal data to see it masked
```

## What the platform does with your data

- Masks e-mail addresses, phone numbers, card numbers, national ID numbers, medical record numbers and
  access tokens before anything is stored (the original never reaches Langfuse).
- Keeps traces for 30 days, then deletes them automatically.
- **Do not rely on this as your only protection.** Avoid sending data you do not need to send.

## If something does not work

| What you see | Most likely cause | What to do |
|---|---|---|
| `ConfigError: Missing setting(s)` | An environment variable is not set in this terminal | Repeat step 2 in the same window you run the agent |
| `certificate verify failed` / `SSL` error | The CA file is missing or is the wrong one | Ask the platform team for the current CA file; check `AGENT_CA_CERT` points to it |
| `401` or `Unauthorized` | Wrong or expired API key | Ask the platform team for the key again |
| `Connection refused` / timeout | Wrong address, firewall, or the collector is down | Check the address and port 4318; ask the platform team to check the collector |
| The agent runs but no trace appears | `provider.shutdown()` was never called, or you looked too early | Add shutdown; wait 30 seconds; refresh |
| Token counts show zero | `record_llm_result` was not called, or your model client did not return usage | Pass the counts your model API returns |

## For the platform team: giving a team access

1. Give the team the collector address (the TLS certificate must cover that name; see `certManager.extraDnsNames` in the chart).
2. Give them the API key through a secure channel (a password manager, not e-mail or chat).
3. Give them the CA file. For the Kubernetes setup it is read from the cluster:
   `kubectl get secret ai-obs-collector-cert -n ai-obs -o jsonpath='{.data.ca\.crt}'` (base64-decode it).
4. Ask them to follow this guide, and check that their traces appear under their team name.

Known limit: today all teams share one agent API key. Separate keys per team (so one team can be cut off
without affecting the others) are planned.

## Example: an agent running on Databricks (NOT TESTED)

We did not have a Databricks workspace to test this on. The steps below follow how Databricks normally works
and use the same settings as above, so treat them as a starting point and report what you find.

1. In a notebook cell: `%pip install opentelemetry-sdk==1.45.2 opentelemetry-exporter-otlp-proto-http==1.45.2`
   then restart the Python process (`dbutils.library.restartPython()`).
2. Store the API key in a Databricks secret scope, not in the notebook. Read it with
   `dbutils.secrets.get(scope="ai-obs", key="agent-api-key")` and put it in `os.environ["AGENT_API_KEY"]`.
3. Set `OTEL_EXPORTER_OTLP_ENDPOINT` the same way. Upload the CA file to a Unity Catalog volume or DBFS and set
   `AGENT_CA_CERT` to its path.
4. Use `platform="databricks"` in `setup_tracing(...)`.
5. Call `provider.shutdown()` at the end of each notebook or job run.
6. The Databricks compute must be allowed to reach the collector address on port 4318 (network rules or private
   connectivity are set by your cloud team).
