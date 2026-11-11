# Handover: Enterprise AI Observability Platform

This document is for the person or team who takes the platform over. It says what exists, what was proven,
what is missing, and what to decide next. It is deliberately honest: the product concept document says it
is "a product concept and prototype plan, not a claim that all capabilities are implemented", and so does this.

## 1. What it is

A secure, open-source layer that lets AI agents running anywhere send a record of what they did (a *trace*), hides
personal data before anything is stored, and lets people inspect the traces, cost and quality in one place.

```
agents (any platform)
   |  OpenTelemetry (OTLP) over HTTPS + API key
   v
collector gateway  ->  checks the key, hides personal data, adds labels, batches, retries
   |
   v
Langfuse (web + worker)  ->  Postgres, ClickHouse, Redis, MinIO
   ^
   |  health numbers
Prometheus -> Grafana + alert rules          cert-manager -> TLS certificate
Argo CD (GitOps) keeps the cluster identical to this Git repository
```

## 2. Versions in use

| Part | Version |
|---|---|
| OpenTelemetry Collector (contrib) | 0.165.0 |
| Langfuse (self-hosted, open-source edition) | 4.x (4.77 when this was written) |
| PostgreSQL / ClickHouse / Redis | 17 / 26.3 / 7 |
| Kubernetes (local test cluster) | kind 0.33 |
| Helm / Argo CD / cert-manager | 4.3.1 / 3.6.1 / 1.18.2 |
| Prometheus / Grafana | 3.5.0 / 11.6.0 |

## 3. Results against the concept document's acceptance criteria

Status words: **Done** (built and tested), **Partly**, **Not done**.

### MVP 1: working trace path
| Criterion | Status | Evidence / note |
|---|---|---|
| A sample agent emits telemetry over OTLP to the collector | Done | Sample agent, a LangGraph agent, and a simulated "other team" agent. **Not a Databricks agent** (none was available) |
| A trace can be found in the UI by its trace ID | Done | Langfuse (see decision 1 below) |
| Parent/child spans show path, timing, status | Done | Seen in Langfuse for all agents |
| Collector health, errors and logs observable | Done | Prometheus, Grafana dashboard, 5 alert rules, collector logs |

### MVP 2: privacy and reliability
| Criterion | Status | Evidence / note |
|---|---|---|
| Redaction rules tested with synthetic sensitive data | Done | Automated redaction tests, plus the live `no_pii_leak` score (docs/DASHBOARDS.md). Names of people are **not** masked |
| TLS and caller authentication | Done | HTTPS with a certificate from cert-manager; API key required |
| Retry, batching, malformed payload, collector restart tested | Done | Automated pipeline tests; load test script |
| Retention, access control, backup, recovery documented | Partly | Retention automated (30 days) and documented. Backup/restore works for the Docker Compose setup only; **no automated backup on Kubernetes**. Access control = Langfuse logins only (no SSO/RBAC) |

### MVP 3: enterprise deployment readiness
| Criterion | Status | Evidence / note |
|---|---|---|
| DNS, TLS, SSO validated in the target environment | Partly | TLS done. DNS and SSO **not done** (everything ran on a laptop cluster via port-forward) |
| GitOps / Argo CD and CI/CD update path tested and documented | Done | Argo CD self-heal and a Git-driven change were demonstrated; CI runs on every push; a version tag publishes the packaged chart. Image-tag changes use the same Git path (not separately exercised) |
| An approved live agent onboarded and verified | Not done | Needs a real approved agent and a real environment; the simulated external agent proves the path only |
| Production-readiness evidence, risks, owners, go/no-go | Partly | Evidence and risks are in this document. **Owners and go/no-go criteria need your organisation** |

### MVP 4: productization and integration
| Criterion | Status | Evidence / note |
|---|---|---|
| Repeatable onboarding documented | Done | docs/ONBOARDING.md, the copy-paste template, a demo agent |
| Evaluate integration with existing platforms | Partly | First comparison in section 6. Needs validation with the real stack |
| Dashboards, alerts, cost and quality reporting | Done (basic) | Grafana dashboard, alerts, cost via model prices, rule-based quality scores (docs/DASHBOARDS.md) |
| Multi-region, HA, tenant isolation, DR | Not done | Out of scope for the single-region MVP |

## 4. Decisions taken

1. **Langfuse instead of Arize Phoenix.** The concept document names Phoenix with PostgreSQL. Langfuse was chosen
   because its core is open source (MIT) and it ingests OpenTelemetry directly. Note that Langfuse reserves some
   features for paid editions (for example its built-in data-retention setting), so retention is done here with
   ClickHouse expiry rules and an object-store rule instead. Confirm licensing with your legal team before commercial use.
2. **Redaction at the gateway, before storage.** Masking only in the UI would not be enough, so the collector masks
   personal data before anything is exported.
3. **Everything as code, applied by Git.** The cluster follows the repository (Argo CD); manual cluster changes are reverted on purpose.
4. **Open-source components only**, apart from the cloud/laptop the cluster runs on.
5. **Simulated agents for testing**, because no Databricks workspace was available.

## 5. Known limitations and risks

| Item | Risk | Suggested next step |
|---|---|---|
| No SSO / role-based access | Anyone with a Langfuse login sees all traces | Add Keycloak (or the company identity provider) with roles |
| Secrets are Kubernetes Secrets created from a `.env` file | Weaker than a vault; `.env` must be guarded | Move to a secrets vault (for example OpenBao) |
| One shared agent API key | One leak affects every team; no per-team cut-off | Per-team keys |
| No alert notifications | Alerts are seen only if someone looks | Install Alertmanager (e-mail/Slack) |
| No automated Kubernetes backup; Prometheus history is temporary | Data loss on cluster failure | Storage snapshots or managed databases; persistent Prometheus volume |
| Single instance of every database; in-memory queue in the collector | Downtime on failure; queued spans lost if the collector dies | High availability; a persistent queue |
| Names of people are not masked | Names in prompts reach storage | Add a name-detection step if policy requires it |
| MinIO image is not pinned (its publisher offers only `latest`) | Unexpected changes on upgrade | Pin by digest |
| Runs on a laptop cluster, reached by port-forward | Not representative of production network and load | Pilot in a real dev environment with DNS and an ingress |
| Never tested with a real Databricks agent | The Databricks notes in the onboarding guide are untested | Pilot with one approved agent |

## 6. Fit with existing platforms (first look)

The concept document asks for a build-versus-integrate decision before committing to a separate product layer.

- **Databricks MLflow Tracing.** Databricks documents that MLflow traces can be exported over standard OTLP to an
  OpenTelemetry-compatible collector, either only to the collector or to both MLflow and the collector
  ([Databricks: Export MLflow traces to OpenTelemetry](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/otel-export)).
  So agents on Databricks can feed this collector **without giving up MLflow**. That supports positioning this
  platform as a cross-platform layer (one redaction point, one place to look) rather than a replacement.
  Not yet verified: the exact attribute mapping (MLflow's span fields to what Langfuse displays).
- **Elastic and Azure Monitor.** Not evaluated yet. To do: confirm what each accepts over OTLP, compare retention,
  access control and cost with the existing enterprise contracts, and check whether prompt/response content can be
  stored there with the same redaction guarantees.
- **Where this platform has a clear edge today:** redaction before storage; one onboarding path for any agent;
  open-source stack the team controls. **Where it does not:** it is a prototype without SSO, HA or support.

Recommendation: run a pilot with one approved Databricks agent exporting over OTLP to this collector, and compare
debugging time against the team's current method, before investing in HA or multi-region.

## 7. Measuring success (fill in from a real run)

| Metric (from the concept document) | How to measure now | Result |
|---|---|---|
| Ingestion success rate | `python scripts/load_test.py --traces 500` (error rate line) | |
| Collector error / drop rate | Grafana "Export failures per second" during the test | |
| Throughput and latency | Same load test (traces per second, p95 latency). Through a port-forward these are a floor, not a ceiling | |
| Privacy effectiveness | `python scripts/langfuse_quality.py score`: share of traces with `no_pii_leak = 1` | |
| Cost transparency | Share of traces showing a cost after model prices are loaded | |
| Debugging time vs baseline | Needs a pilot with real users | |

## 8. Where things are

| Need | File |
|---|---|
| Overview and roadmap | README.md |
| Run it, fix it | docs/RUNBOOK.md |
| Connect another team's agent | docs/ONBOARDING.md |
| Cost, quality and health views | docs/DASHBOARDS.md |
| Present it | docs/DEMO.md |
| Build log with every command | docs/STEPS.md |
| Cluster packaging | deploy/helm/ai-observability/ |
| GitOps | deploy/argocd/ |
| Collector pipeline and redaction rules | collector/config.yaml |
| Tools | scripts/ (secrets, certificates, retention, backup, load test, cost and quality) |
| Tests | tests/ |
