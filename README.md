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
