# Build log: step-by-step details (Steps 1 to 7)

This is the detailed, step-by-step record of how each part was built and verified. For the overview, see the [main README](../README.md). Commands assume the repository root as the working directory.

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

**5d. Retention (done).** `python scripts/retention.py plan --days 30` shows the rules; `apply` sets them on the running stack; `status` shows what is active; `remove` keeps data forever again. Default days: `RETENTION_DAYS` in `.env` (30).
ClickHouse deletes traces/observations/scores older than N days row by row (exact, not rounded to months); MinIO lifecycle rules expire the raw event JSON and media. Tested against Langfuse's real ClickHouse schema.

**5e. Backup and restore (done).**
```bash
python scripts/backup.py backup --keep 7      # ~1 minute downtime; writes backups/<timestamp>/ with checksums
python scripts/backup.py list
python scripts/backup.py verify backups/<timestamp>
python scripts/backup.py restore backups/<timestamp> --yes    # REPLACES current data
```
Backups hold your data (possibly sensitive text): store them encrypted and off this machine. `backups/` is git-ignored.
Schedule: Windows Task Scheduler / cron running `python scripts/backup.py backup --keep 7` nightly.

Step 5 complete.

## Step 2 — real agent (LangGraph + local LLM)
`agent/langgraph_agent.py` is an agent that DECIDES which tool to use: plan -> tool -> plan -> ... -> answer (max 4 tool calls).
Tools: `calculator` (safe, no `eval`), `lookup` (small knowledge base), `clock`.
```bash
pip install -r agent/requirements.txt        # adds langgraph
ollama pull qwen2.5:3b                       # once; set OLLAMA_MODEL=qwen2.5:3b in .env
cd agent
python langgraph_agent.py --question "What time is it, and what is 12 * 7?"
python langgraph_agent.py --fail             # calculator breaks; agent reports it instead of crashing
python langgraph_agent.py --pii              # FAKE sensitive data; check Langfuse shows [REDACTED_*]
python langgraph_agent.py --mock             # scripted LLM, no Ollama needed
```
Traces go through the same gateway (API key, TLS, redaction) and show in Langfuse with tags `step-2`, `langgraph`.
Gateway fix found while building this: the redaction rule that drops secret-named fields also deleted `gen_ai.usage.input_tokens`/`output_tokens`; those counts are now kept (needed for cost tracking).

## Step 6 — Kubernetes + Helm
The whole platform as a Helm chart: `deploy/helm/ai-observability` (collector gateway, Langfuse web + worker, Postgres, ClickHouse, Redis, MinIO, nightly retention job, network policies).
Passwords never appear in the chart: pods read them from a Kubernetes Secret created from your `.env` (`scripts/k8s_secrets.py`). The collector config is the SAME file as Compose uses (a test fails if the copies drift).

Try it on your laptop with a throwaway kind cluster (stop Compose first, they use the same ports and RAM):
```bash
docker compose stop                                   # NOT "down": keeps your data
kind create cluster --name ai-obs --config deploy/kind/kind-config.yaml
kubectl create namespace ai-obs
python scripts/k8s_secrets.py --namespace ai-obs | kubectl apply -f -
helm install ai-observability deploy/helm/ai-observability -n ai-obs -f deploy/helm/ai-observability/values-kind.yaml
kubectl get pods -n ai-obs -w                         # wait until all are Running/Ready (first start: ~5 min)
kubectl port-forward -n ai-obs svc/ai-observability-langfuse-web 3000:3000     # window 1: UI
kubectl port-forward -n ai-obs svc/ai-observability-collector 4318:4318        # window 2: agents
```
The agents work unchanged (`https://localhost:4318`, same API key and CA).
Remove everything: `kind delete cluster --name ai-obs`, then `docker compose start`.

Known gaps (planned): MinIO image is unpinned (Chainguard publishes only `latest`; pin by digest later); databases are single-instance (managed Postgres/ClickHouse for production comes with the enterprise values in Step 8); TLS certificate is the dev CA (cert-manager in Step 8).

## Step 7 — GitOps with Argo CD
Argo CD watches this GitHub repo and keeps the cluster identical to the Helm chart (`deploy/argocd/application.yaml`). Push a chart change to `main` and it is rolled out automatically; manual changes to the cluster are reverted (`selfHeal`), and things removed from Git are removed from the cluster (`prune`). Deleting the Argo app never deletes your data (no cascade finalizer). Secrets stay out of Git (created by `scripts/k8s_secrets.py`; a vault in Step 8).
```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.1/manifests/install.yaml
kubectl scale -n argocd deployment/argocd-dex-server deployment/argocd-notifications-controller --replicas=0   # save RAM on a laptop
kubectl apply -f deploy/argocd/application.yaml
kubectl port-forward -n argocd svc/argocd-server 8080:443          # UI: https://localhost:8080 (user: admin)
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}"   # base64 of the admin password
```

## Tests (Step 4)
Automated tests start a real collector with `collector/config.yaml` (on ports 14xxx, so they never clash with your running stack) and a fake Langfuse that records what the collector exports.

```bash
cd agent && python -m venv .venv && .venv\Scripts\activate      # once (Windows; use source .venv/bin/activate on Mac/Linux)
cd .. && pip install -r tests/requirements.txt
python -m pytest                                                  # needs Docker running; takes ~3 minutes
```
| Group | What it proves |
|---|---|
| Redaction (22 tests) | 15 kinds of sensitive text are masked; nested JSON, error messages, stack traces and resource attributes are covered; secret-named fields are dropped (but LLM `*_tokens` usage counts are kept); normal text is untouched; oversized values are cut |
| Security (12 tests) | agents without/with a wrong API key get 401 and nothing is stored; health probes stay open; plain HTTP and untrusted certificates are refused; spans arrive over TLS |
| Secrets (12 tests) | placeholders/defaults/short/reused secrets are detected; generated secrets are strong and unique; compose has no weak fallbacks and refuses to start without secrets; no keys/tokens are committed |
| Retention (15 tests) | TTL SQL targets the right tables/columns, rejects bad input, and **really deletes old rows in an embedded ClickHouse** (needs `pip install chdb`, otherwise skipped) |
| Backups (9 tests) | stack restarts even if archiving fails; partial backups are removed; corruption is detected; restore needs `--yes`, an intact backup and matching secrets; never uses `down -v` |
| LangGraph agent (see Step 2) | safe calculator refuses code/huge numbers; untrusted model output can't pick unknown tools; the plan-tool loop is bounded and can't repeat a call; every span is a child of `agent.run`; tool failures are recorded and the agent recovers; PII is redacted for these traces too |
| Helm chart (24 tests) + k8s secrets (6) | chart lints and renders; no password is ever a literal; every secret key exists and is used; `$(VAR)` references are valid; selectors match pods; collector hardened (non-root, read-only filesystem); databases reachable only from Langfuse; retention tables = `scripts/retention.py`; the collector config copy equals `collector/config.yaml`; k8s secrets refuse weak values and never include the CA private key |
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
collector/config.yaml     Collector pipeline (auth, TLS, redaction)
deploy/helm/ai-observability/   Helm chart (Step 6)
deploy/kind/kind-config.yaml    Local test cluster
deploy/argocd/application.yaml  GitOps: Argo CD app that deploys the chart from GitHub
scripts/k8s_secrets.py    .env + certs -> Kubernetes Secrets
agent/agent.py            Step 1 scripted sample agent (Ollama or mock LLM)
agent/langgraph_agent.py  Step 2 LangGraph agent with tools
.env.example              Secrets and bootstrap keys
```
