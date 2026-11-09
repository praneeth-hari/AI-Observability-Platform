# Enterprise AI Observability — Step 1: working trace path

```
sample agent ──OTLP──► OTel Collector gateway ──OTLP/HTTP + auth──► Langfuse
 (Python + OTel SDK)    (enrich, batch, retry)                     (Postgres · ClickHouse · Redis · MinIO)
```

All components are open source: OpenTelemetry (Apache 2.0), Langfuse (MIT), PostgreSQL, ClickHouse, Redis, MinIO, Ollama.

## Prerequisites
- Docker Desktop (or Docker Engine + Compose v2), about 8 GB RAM free
- Python 3.10+
- Optional: [Ollama](https://ollama.com) with a small model: `ollama pull qwen2.5:1.5b`
  (without Ollama the agent uses a mock LLM, so the trace path still works)

## Run
```bash
cp .env.example .env          # then edit the CHANGEME values
docker compose up -d
docker compose ps             # wait until langfuse-web is up (first start takes 1–3 min)
```
Open http://localhost:3000 and log in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`.

Send traces:
```bash
cd agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python agent.py                      # normal run
python agent.py --fail               # failing tool call -> red error span
```
The script prints a **Trace ID**. In Langfuse go to **Tracing**, then search or open that trace.

## What "done" looks like (MVP 1 acceptance)
- [ ] A trace is visible in Langfuse and can be found by its trace ID
- [ ] Tree shows `agent.run` → `llm.planner` (generation) → `tool.calculator` (tool) → `llm.answer` (generation), with timings
- [ ] `--fail` run shows the tool span and root span as ERROR with the exception message
- [ ] Token usage is shown on generations (with Ollama)
- [ ] Collector health: `curl localhost:13133`
- [ ] Collector metrics: `curl -s localhost:8888/metrics | grep -E "accepted_spans|sent_spans|failed"`
- [ ] Collector logs: `docker compose logs -f otel-collector`

## Step 3 — PII redaction at the gateway
The collector (`collector/config.yaml`, processor `transform/redact`) cleans every span **before** it is exported or stored:
- drops attributes whose *name* looks like a secret (password, token, authorization, api key, cookie)
- masks sensitive *values*: emails, phone numbers, card numbers, SSNs, Aadhaar-style IDs, MRNs, bearer tokens, API keys
- also cleans exception messages, stack traces and span status messages
- cuts any attribute value longer than 8000 characters

Try it (all data is fake):
```bash
docker compose restart otel-collector
python agent.py --pii --mock           # question contains fake email/phone/card/SSN/MRN
python agent.py --pii --fail --mock    # the error message also contains fake PII
```
In Langfuse the input should read `My email is [REDACTED_EMAIL], phone [REDACTED_PHONE], card [REDACTED_CARD] ...`.

## Tests (Step 4)
Automated tests start a real collector with `collector/config.yaml` (on ports 14xxx, so they never clash with your running stack) and a fake Langfuse that records what the collector exports.

```bash
cd agent && python -m venv .venv && .venv\Scripts\activate      # once (Windows; use source .venv/bin/activate on Mac/Linux)
cd .. && pip install -r tests/requirements.txt
python -m pytest                                                  # needs Docker running; takes ~3 minutes
```
| Group | What it proves |
|---|---|
| Redaction (21 tests) | 15 kinds of sensitive text are masked; nested JSON, error messages, stack traces and resource attributes are covered; secret-named fields are dropped; normal text is untouched; oversized values are cut |
| Pipeline (5 tests) | traces keep their parent/child structure; gateway adds environment labels; garbage input is rejected without crashing; spans survive a backend outage (retry); the collector works after a restart |

GitHub runs the same tests on every push (`.github/workflows/ci.yml`).
Known limit: spans waiting in the collector's in-memory queue are lost if the collector is killed mid-send (a persistent queue is planned for Step 5).

## Troubleshooting
| Symptom | Check |
|---|---|
| Agent prints trace ID but nothing in Langfuse | `docker compose logs otel-collector`; a 401 means the keys in `.env` don't match the project |
| Collector `connection refused` to langfuse-web | Langfuse still starting; the collector retries for 5 minutes |
| Langfuse won't start | `docker compose logs langfuse-web langfuse-worker`; usually ClickHouse/MinIO not healthy yet |
| Reset everything | `docker compose down -v` (deletes all data) |

## Layout
```
docker-compose.yml        Langfuse stack + collector gateway
collector/config.yaml     Collector pipeline (Step 2 adds redaction here)
agent/agent.py            Instrumented sample agent (Ollama or mock LLM)
.env.example              Secrets and bootstrap keys
```
