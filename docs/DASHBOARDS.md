# Dashboards: platform health, cost and quality

Three different questions, three places to look.

| Question | Where | Needs |
|---|---|---|
| Is the platform itself healthy? | Grafana, dashboard **AI Observability - Collector health** | Nothing extra |
| How much do the agents cost, and how fast are they? | Langfuse, **Dashboards** and the **Cost** quick filter on **Tracing** | Model prices (step 1 below) |
| Are the agents' answers good? | Langfuse, **Scores** (left menu) and **Dashboards** | Quality scores (step 2 below) |

## Cost

Langfuse multiplies the token counts on each trace by a price per token. Agents already send token counts
(`gen_ai.usage.input_tokens` and `...output_tokens`). The prices are the missing piece.

1. Open the price list `deploy/langfuse/model-prices.json` and put in your real prices (USD per token).
   The file ships with local models at 0 and a **demo** price for the onboarding demo agent's `fake-llm`.
2. Start the Langfuse port-forward (see the runbook), and set the secret key once in your window:
   `$env:LANGFUSE_SECRET_KEY = "<the project secret key>"` (it is `LANGFUSE_INIT_PROJECT_SECRET_KEY` in your `.env`;
   the script reads `.env` by itself when you run it from the project folder).
3. See what would happen, then do it:
```
python scripts/langfuse_quality.py models
python scripts/langfuse_quality.py models --apply
```
4. Send a new trace (for example the demo agent). In Langfuse open **Tracing**: the **Cost** column and the **Cost**
   quick filter now show values. Open **Dashboards** for cost and latency by model and over time.

Only new traces get a cost. Cost is shown only where a price exists, and it is only as good as the token counts the
agent reports. Provider prices change: keep the file under version control and review it.

## Quality

Quality scores are simple, automatic checks written to each trace (1 = good, 0 = bad):

| Score | Meaning |
|---|---|
| `answer_present` | The agent produced an answer |
| `not_failed` | The run did not end in an error |
| `fast_enough` | The run finished within the limit (default 10 seconds) |
| `no_pii_leak` | No raw e-mail, card or national-ID pattern is left in the stored text. This is a live check that redaction worked |

```
python scripts/langfuse_quality.py score                      # shows what it would add (last 24 hours)
python scripts/langfuse_quality.py score --apply              # writes the scores
python scripts/langfuse_quality.py score --hours 2 --name order-support-agent --max-latency 5 --apply
```

Then in Langfuse: **Scores** in the left menu lists them, **Tracing** can be filtered by score, and **Dashboards**
can chart score averages over time. Run the command on a schedule (for example hourly) to keep the charts current.
Re-running never creates duplicates.

These are rule-based checks, not a judgement of whether an answer is *correct*. Judging correctness needs a reference
answer or a reviewer; Langfuse supports that through **Evaluators** and **Human annotation**, which we have not set up.

## Platform health

Grafana shows the collector's own numbers: up or down, spans received and delivered per second, failures,
retry queue and memory. The Prometheus **Alerts** page shows the alert rules. See docs/RUNBOOK.md for what to do about each.

## Load and capacity

`python scripts/load_test.py` measures how many traces per second the collector accepts and how long it takes
(see the top of that file). Run it with Grafana open and watch the "Spans received per second" and
"Retry queue" graphs react.
