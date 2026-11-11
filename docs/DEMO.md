# Demo script (about 15 minutes)

Plain-language talking points are in *italics*. Commands are for PowerShell in the project folder.

## Before the demo (10 minutes earlier)

1. Docker Desktop is running; `kubectl get pods -n ai-obs` shows every pod `1/1 Running`.
2. Open four windows, each running one port-forward from docs/RUNBOOK.md section 2:
   Langfuse (3000), Grafana (3001), Prometheus (9091), collector (4318). Add `kubectl port-forward -n ai-obs svc/ai-observability-collector 8888:8888` as a fifth only if you want the live counters in the load test.
3. In a sixth window, set the agent settings (the key is read from the cluster, not typed):
```
$k = kubectl get secret ai-obs-secrets -n ai-obs -o jsonpath="{.data.AGENT_API_KEY}"
$env:AGENT_API_KEY = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($k))
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "https://localhost:4318"
$env:AGENT_CA_CERT = "certs\k8s-ca.crt"
$env:LANGFUSE_SECRET_KEY = "<project secret key>"
```
4. Browser tabs: Langfuse **Tracing**, Grafana **Collector health**, Prometheus **Alerts**.
5. Close other heavy programs (the cluster needs the memory).

## 1. The problem (1 minute)

*"Companies run AI agents on several platforms. When one fails, is slow or costs too much, nobody can see why, and
the prompts often contain personal data. This platform gives one safe place to look."*

## 2. It is running like a real service (2 minutes)

```
kubectl get pods -n ai-obs
kubectl get applications -n argocd
kubectl get certificate -n ai-obs
```
*"Everything runs on Kubernetes. Argo CD keeps it identical to the Git repository. The TLS certificate is issued and renewed automatically."*

## 3. Another team connects an agent (3 minutes)

```
python onboarding/example_external_agent.py --question "Where is order 1042?"
```
In Langfuse **Tracing**: open the newest `order-support-agent`; show the tool step and the model step with tokens.
*"This agent comes from a different team. They added one file and three environment variables. That is the whole onboarding."*

## 4. Personal data never reaches storage (2 minutes)

```
python onboarding/example_external_agent.py --pii
```
Open the new trace, then the `llm.answer` step: the input shows `[REDACTED_EMAIL]` and `[REDACTED_PHONE]`.
*"The agent sent a real-looking e-mail and phone number. The gateway masked them before anything was saved. Names of people are not masked yet; that is on the list."*

## 5. Platform health, under load (3 minutes)

Open Grafana **Collector health** (range: Last 15 minutes), then:
```
python scripts/load_test.py --traces 300 --workers 4
```
The "Spans received per second" and "Spans delivered" graphs rise together; "Export failures" stays empty.
Read the `RESULT: PASS` line and the traces-per-second figure aloud.
*"Through a laptop port-forward the numbers are a floor, not the limit."*

## 6. An alert fires, and we fix it (3 minutes)

Pause Argo CD's automatic repair, then switch the collector off:
```
$f = "$env:TEMP\heal-off.json"
Set-Content -Path $f -Value '{"spec":{"syncPolicy":{"automated":{"selfHeal":false}}}}'
kubectl patch application ai-observability -n argocd --type merge --patch-file $f
kubectl scale deploy ai-observability-collector -n ai-obs --replicas=0
```
After about 2 minutes **CollectorDown** turns red on the Prometheus Alerts page and Grafana says DOWN. Then restore:
```
kubectl scale deploy ai-observability-collector -n ai-obs --replicas=1
Set-Content -Path $f -Value '{"spec":{"syncPolicy":{"automated":{"selfHeal":true}}}}'
kubectl patch application ai-observability -n argocd --type merge --patch-file $f
```
*"Alerts fire within about a minute. The runbook says what to do for each one."*
Tip: start the scale-down at the beginning of section 5 so the alert is ready when you get here.

## 7. Cost and quality (1 minute)

```
python scripts/langfuse_quality.py models --apply
python scripts/langfuse_quality.py score --apply
```
Show the **Cost** column in Tracing and the **Scores** page. *"Cost appears once prices are loaded. Quality checks include a live test that no personal data is left in storage."*

## 8. Close (1 minute): honest status

*"Working and tested: tracing, redaction, TLS, GitOps, monitoring, onboarding. Not done yet: single sign-on, a secrets vault, high availability, notifications, and a test with a real Databricks agent."*
Point to docs/HANDOVER.md for the full list and the recommended pilot.

## After the demo

- Confirm `kubectl get applications -n argocd` says Synced and Healthy and the collector has 1 replica.
- Close the port-forward windows.
