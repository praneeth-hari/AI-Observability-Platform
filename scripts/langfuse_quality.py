"""
Cost and quality helpers for Langfuse (talks to Langfuse's public API).

  python scripts/langfuse_quality.py models            # SHOW the model prices it would create
  python scripts/langfuse_quality.py models --apply    # create them in Langfuse (so cost appears)
  python scripts/langfuse_quality.py score             # SHOW the quality scores it would add
  python scripts/langfuse_quality.py score --apply     # add them to the traces

Nothing is changed unless you pass --apply.

models: reads deploy/langfuse/model-prices.json and creates any model that Langfuse does not know yet.
        Langfuse then multiplies the token counts on each trace by these prices to show cost.
score:  looks at recent traces and adds simple, automatic quality scores (1 = good, 0 = bad):
          answer_present  the agent produced an answer
          not_failed      the run did not end in an error
          fast_enough     the run finished within --max-latency seconds
          no_pii_leak     NO raw e-mail, card or SSN pattern is left in the stored text
                          (a live check that the collector's redaction worked)
        Re-running is safe: each score has a fixed id, so it is updated, not duplicated.

Settings (environment or .env): LANGFUSE_HOST (default http://localhost:3000, needs a port-forward),
LANGFUSE_PUBLIC_KEY (default pk-lf-local-prototype), LANGFUSE_SECRET_KEY or LANGFUSE_INIT_PROJECT_SECRET_KEY.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PRICES = ROOT / "deploy" / "langfuse" / "model-prices.json"

LEAK_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}


def load_dotenv(path):
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip().strip('"').strip("'"))
    except OSError:
        pass


class Langfuse:
    def __init__(self, host, public_key, secret_key):
        self.host = host.rstrip("/")
        self.http = requests.Session()
        self.http.auth = (public_key, secret_key)
        self.http.headers["Content-Type"] = "application/json"

    def get(self, path, **params):
        r = self.http.get(self.host + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = self.http.post(self.host + path, data=json.dumps(body), timeout=30)
        r.raise_for_status()
        return r.json() if r.content else {}

    def pages(self, path, **params):
        page = 1
        while True:
            data = self.get(path, page=page, **params)
            yield from data.get("data", [])
            if page >= data.get("meta", {}).get("totalPages", 1):
                return
            page += 1


# ---------------- quality rules (pure functions, unit-tested) ----------------
def _text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def find_leaks(*texts):
    """Names of the sensitive patterns still visible in the given texts."""
    joined = " ".join(_text(t) for t in texts)
    return sorted(name for name, rx in LEAK_PATTERNS.items() if rx.search(joined))


def score_trace(trace, max_latency):
    """Return a list of (name, value 0/1, comment) for one trace as returned by the Langfuse API."""
    output = _text(trace.get("output")).strip()
    scores = [
        ("answer_present", 1 if output and not output.startswith("FAILED") else 0,
         "has an answer" if output and not output.startswith("FAILED") else "no answer"),
        ("not_failed", 0 if output.startswith("FAILED") else 1, output[:120] if output.startswith("FAILED") else "ok"),
    ]
    latency = trace.get("latency")
    if latency is not None:
        scores.append(("fast_enough", 1 if latency <= max_latency else 0, f"{latency:.2f}s (limit {max_latency}s)"))
    leaks = find_leaks(trace.get("input"), trace.get("output"))
    scores.append(("no_pii_leak", 0 if leaks else 1, ("LEAK: " + ", ".join(leaks)) if leaks else "no raw PII patterns found"))
    return scores


def score_requests(trace, max_latency):
    return [{"id": f"{trace['id']}-{name}", "traceId": trace["id"], "name": name, "value": value,
             "dataType": "BOOLEAN", "comment": comment}
            for name, value, comment in score_trace(trace, max_latency)]


# ---------------- commands ----------------
def cmd_models(lf, args):
    wanted = json.loads(Path(args.file).read_text(encoding="utf-8"))["models"]
    existing = {m.get("modelName") for m in lf.pages("/api/public/models", limit=100)}
    todo = [m for m in wanted if m["modelName"] not in existing]
    for m in wanted:
        state = "create" if m in todo else "exists"
        print(f"  {state:7s} {m['modelName']:22s} in {m['inputPrice']} / out {m['outputPrice']} USD per token")
    if args.apply:
        for m in todo:
            lf.post("/api/public/models", m)
        print(f"Created {len(todo)} model price(s).")
    else:
        print(f"Dry run: {len(todo)} would be created. Add --apply to create them.")
    return 0


def cmd_score(lf, args):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)).isoformat().replace("+00:00", "Z")
    params = {"limit": 50, "fromTimestamp": since}
    if args.name:
        params["name"] = args.name
    traces = list(lf.pages("/api/public/traces", **params))
    sent = bad = 0
    for t in traces:
        for req in score_requests(t, args.max_latency):
            if req["value"] == 0:
                bad += 1
                print(f"  BAD  {t['id'][:12]}  {req['name']:15s} {req['comment']}")
            if args.apply:
                lf.post("/api/public/scores", req)
                sent += 1
    print(f"{len(traces)} trace(s) checked, {bad} failed check(s)"
          + (f", {sent} score(s) written." if args.apply else ". Dry run: add --apply to write the scores."))
    return 0


def main(argv=None):
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.getenv("LANGFUSE_HOST", "http://localhost:3000"))
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("models")
    m.add_argument("--file", default=str(PRICES))
    m.add_argument("--apply", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--name", help="only traces with this trace name")
    s.add_argument("--max-latency", type=float, default=10.0, help="seconds")
    s.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    secret = os.getenv("LANGFUSE_SECRET_KEY") or os.getenv("LANGFUSE_INIT_PROJECT_SECRET_KEY", "")
    public = os.getenv("LANGFUSE_PUBLIC_KEY") or os.getenv("LANGFUSE_INIT_PROJECT_PUBLIC_KEY") or "pk-lf-local-prototype"
    if not secret:
        sys.exit("No Langfuse secret key: set LANGFUSE_SECRET_KEY (or LANGFUSE_INIT_PROJECT_SECRET_KEY in .env).")
    lf = Langfuse(a.host, public, secret)
    try:
        return {"models": cmd_models, "score": cmd_score}[a.command](lf, a)
    except requests.HTTPError as e:
        sys.exit(f"Langfuse answered {e.response.status_code}: {e.response.text[:200]}")
    except requests.ConnectionError:
        sys.exit(f"Cannot reach Langfuse at {a.host}. Is the port-forward running (kubectl port-forward ... 3000:3000)?")


if __name__ == "__main__":
    sys.exit(main())
