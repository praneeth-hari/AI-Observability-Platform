# Runbook: what to do when something breaks

For whoever is on call for the platform. Every command is for PowerShell with `kubectl` pointed at the
cluster (`kubectl config current-context` should say `kind-ai-obs` for the local setup).
Namespace: `ai-obs`. Release name: `ai-observability`, so pods are named `ai-observability-<part>-...`.

## 1. Quick health check (30 seconds)

```
kubectl get pods -n ai-obs
kubectl get applications -n argocd
kubectl get certificate -n ai-obs
```

Healthy means: every pod `1/1 Running` (the `retention` pod shows `Completed`, which is normal),
the application `Synced` and `Healthy`, and both certificates `READY True`.

## 2. How to open the screens

Each command needs its own window and keeps running; close it with `Ctrl+C`.

| Screen | Command | Address |
|---|---|---|
| Langfuse (traces) | `kubectl port-forward -n ai-obs svc/ai-observability-langfuse-web 3000:3000` | http://localhost:3000 |
| Grafana (health dashboard) | `kubectl port-forward -n ai-obs svc/ai-observability-grafana 3001:3000` | http://localhost:3001 |
| Prometheus (alerts) | `kubectl port-forward -n ai-obs svc/ai-observability-prometheus 9091:9090` | http://localhost:9091/alerts |
| Collector (agents send here) | `kubectl port-forward -n ai-obs svc/ai-observability-collector 4318:4318` | https://localhost:4318 |

Grafana login: user `admin`, password = the Langfuse admin password. Read it without printing it to a log:

```
$p = kubectl get secret ai-obs-secrets -n ai-obs -o jsonpath="{.data.LANGFUSE_INIT_USER_PASSWORD}"
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p))
```

(Tip: PowerShell splits very long pasted lines. Put long values in a variable first.)

## 3. Alerts: what each one means and what to do

Alerts show on the Prometheus **Alerts** page and in Grafana. There is no e-mail or Slack notification yet
(see Known limits).

### CollectorDown (critical): nobody can send traces

Prometheus cannot reach the collector.

1. `kubectl get pods -n ai-obs -l app.kubernetes.io/component=collector`
2. If there is no pod, or it is not `Running`: `kubectl describe pod -n ai-obs -l app.kubernetes.io/component=collector`
   and look at **Events** at the bottom.
3. Read its log: `kubectl logs deploy/ai-observability-collector -n ai-obs --tail=100`

| Finding | Fix |
|---|---|
| Pod stuck in `ContainerCreating`, event mentions a missing secret | `kubectl get certificate -n ai-obs`. If not `True`, see "Certificates" below |
| `OOMKilled` | Raise `collector.resources.limits.memory` in `values-kind.yaml`, push to Git; Argo CD applies it |
| Config error in the log | Revert the last change to `collector/config.yaml` in Git |
| Zero replicas | `kubectl scale deploy ai-observability-collector -n ai-obs --replicas=1` (Argo CD self-heal normally does this within a minute) |

### ExportFailing (critical): the collector cannot deliver traces to Langfuse

The collector retries for up to 5 minutes, then **drops** the data. Act quickly.

1. `kubectl get pods -n ai-obs` : are `langfuse-web`, `langfuse-worker`, `clickhouse`, `postgres`, `redis`, `minio` all `1/1 Running`?
2. `kubectl logs deploy/ai-observability-langfuse-web -n ai-obs --tail=100`
3. A restarting pod: `kubectl describe pod -n ai-obs <pod-name>`. `OOMKilled` or "heap out of memory"
   means raise that component's memory limit (and `nodeHeapMb`, which must stay below the limit) in the values file.
4. Once Langfuse is healthy again, the alert clears by itself after a few minutes.

### ExportQueueFilling (warning): Langfuse is slow or down

The collector keeps up to 5000 batches in memory while it retries. Treat it like ExportFailing, one step earlier.
The queue lives in memory: if the collector restarts now, what is queued is lost.

### SpansRefused (warning): the collector turns data away

It starts refusing when its memory use reaches about 80% of its limit. Check `CollectorMemoryHigh`, reduce the
traffic, or raise the memory limit / add replicas (`collector.replicas`).

### CollectorMemoryHigh (warning): above 300 MB for 5 minutes

Raise the limit, add a second replica, or find the agent that sends unusually large payloads
(payload fields are cut at 8000 characters).

## 4. Routine tasks

**Restart a component** (safe, takes a minute):
`kubectl rollout restart deploy/ai-observability-collector -n ai-obs` (same for `-langfuse-web`, `-langfuse-worker`).
Databases (`postgres`, `clickhouse`, `redis`, `minio`) are StatefulSets: `kubectl rollout restart statefulset/ai-observability-postgres -n ai-obs`.

**Argo CD is not applying a change:**
`kubectl get applications -n argocd`, then ask it to look now:
```
$a = "argocd.argoproj.io/refresh=hard"
kubectl annotate application ai-observability -n argocd $a --overwrite
```
If it shows `OutOfSync` for long, open the application's conditions: `kubectl describe application ai-observability -n argocd`.
Manual changes in the cluster are reverted on purpose (self-heal). Change Git, not the cluster.

**Certificates.** cert-manager renews the collector certificate 30 days before it expires, and the collector re-reads
it every hour. To check: `kubectl get certificate -n ai-obs`. To force a new one:
`kubectl delete secret ai-obs-collector-cert -n ai-obs` (cert-manager recreates it in seconds), then
`kubectl rollout restart deploy/ai-observability-collector -n ai-obs`. After a new certificate **authority** is created
(for example a rebuilt cluster), every agent needs the new CA file; see docs/ONBOARDING.md, "For the platform team".

**Rotate the agent API key** (do this if it was exposed, and on a schedule):
1. Put a new long random value in `AGENT_API_KEY` in your `.env` file (never in Git).
2. `python scripts/k8s_secrets.py --namespace ai-obs | kubectl apply -f -` (rewrites the secrets from `.env`; the other values stay the same)
3. `kubectl rollout restart deploy/ai-observability-collector -n ai-obs`
4. Give the new key to the teams through a secure channel. Their agents get `401` until they switch.

**Retention (deleting old data).** A daily job deletes data older than the configured days (30 by default).
Check: `kubectl get cronjob -n ai-obs`. Run it now:
`kubectl create job --from=cronjob/ai-observability-retention retention-manual -n ai-obs`, then
`kubectl logs job/retention-manual -n ai-obs`. Remove it afterwards: `kubectl delete job retention-manual -n ai-obs`.

**Forgot the Langfuse or Grafana password:** the command in section 2.

## 5. Laptop-specific problems (kind cluster on Docker Desktop)

| Symptom | Fix |
|---|---|
| `kubectl` says connection refused / EOF after Docker restarted | `docker start ai-obs-control-plane`, wait a minute |
| Port not reachable even though the node is running | `docker stop ai-obs-control-plane` then `docker start ai-obs-control-plane` |
| "bind: forbidden" or port reserved errors on Windows | Run `net stop winnat` then `net start winnat` in an administrator terminal |
| Pods keep restarting or are `Pending` | `docker stats --no-stream` : Docker Desktop's memory limit is probably full. Stop other containers or raise the limit in Docker Desktop settings |
| `kubectl port-forward` says "address already in use" | An older forward is still running in another window. Close it |

## 6. What to send when you ask for help

```
kubectl get pods -n ai-obs
kubectl get applications -n argocd
kubectl describe pod -n ai-obs <the failing pod>
kubectl logs <the failing pod> -n ai-obs --tail=100
kubectl get events -n ai-obs --sort-by=.lastTimestamp
```

Never paste passwords, API keys or the `.env` file.

## 7. Known limits that affect operations

- **Backups.** `scripts/backup.py` covers the Docker Compose setup only. The Kubernetes (kind) data lives on the
  cluster's own volumes and has **no automated backup**. For production use storage snapshots or a managed database.
- **No notifications.** Alerts are visible on screens only. Alertmanager (e-mail/Slack) is not installed.
- **Prometheus history** is kept on a temporary volume and is lost when its pod is replaced.
- **Single copy.** Databases run as one instance each. A node failure means downtime.
- **One shared agent key** for all teams.
