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

## Step 5 — Security (in progress)
**5a. Agent authentication (done).** The collector only accepts telemetry that carries `Authorization: Bearer <AGENT_API_KEY>`.
Set `AGENT_API_KEY` in `.env` (long random value). `agent.py` reads it from `.env` automatically. A missing or wrong key gets `401`, and nothing is stored.
The `:13133` health endpoint stays open so Kubernetes/monitoring probes work.

**5b. TLS encryption (done).** The collector only accepts HTTPS/TLS 1.2+ on ports 4317/4318, so the API key and traces are never sent in clear text.
```bash
pip install cryptography
python scripts/make_certs.py          # creates certs/ (git-ignored): ca.crt, server.crt, server.key, ca.key
docker compose up -d --force-recreate otel-collector
```
Agents trust `certs/ca.crt` (path in `AGENT_CA_CERT`, defaults to `certs/ca.crt`) and use `https://localhost:4318`.
These are *development* certificates. Production uses the company CA / cert-manager (Step 8).
Gotcha: if the environment variable `REQUESTS_CA_BUNDLE` is set (common on company laptops), the Python `requests` library uses it instead of the agent's CA file.

**5c. Secrets handling (done).**
- `docker-compose.yml` has **no fallback passwords**: it refuses to start if a secret is missing from `.env`.
- `python scripts/secrets_tool.py check` audits `.env` (placeholders, well-known defaults, too short, reused values). Exit code 1 = weak.
- `python scripts/secrets_tool.py generate` writes `.env.generated` with strong random secrets (never overwrites `.env`). After renaming it to `.env`, run `docker compose down -v` then `docker compose up -d`, because database passwords are baked into the data volumes (this deletes stored traces).
- Tests fail the build if a private key, API token or weak compose default is committed.
- Real deployments: secrets come from a secret manager (OpenBao/Kubernetes secrets, Step 8), not a file.

Next: 5d retention, 5e backups.

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
| Security (12 tests) | agents without/with a wrong API key get 401 and nothing is stored; health probes stay open; plain HTTP and untrusted certificates are refused; spans arrive over TLS |
| Secrets (12 tests) | placeholders/defaults/short/reused secrets are detected; generated secrets are strong and unique; compose has no weak fallbacks and refuses to start without secrets; no keys/tokens are committed |
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
