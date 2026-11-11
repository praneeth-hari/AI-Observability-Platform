"""Tests for scripts/langfuse_quality.py. A tiny fake Langfuse server stands in for the real one."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import langfuse_quality as q  # noqa: E402

GOOD = {"id": "t-good", "name": "order-support-agent", "latency": 1.2,
        "input": "I am [REDACTED_EMAIL], phone [REDACTED_PHONE]. Where is order 1042?", "output": "shipped"}


# ---- rules ----
def by_name(trace, max_latency=10):
    return {n: (v, c) for n, v, c in q.score_trace(trace, max_latency)}


def test_a_healthy_trace_scores_one_everywhere():
    s = by_name(GOOD)
    assert {k: v[0] for k, v in s.items()} == {"answer_present": 1, "not_failed": 1, "fast_enough": 1, "no_pii_leak": 1}


@pytest.mark.parametrize("output", [None, "", "   "])
def test_an_empty_answer_is_flagged(output):
    assert by_name({**GOOD, "output": output})["answer_present"][0] == 0


def test_a_failed_run_is_flagged():
    s = by_name({**GOOD, "output": "FAILED: tool crashed"})
    assert s["not_failed"][0] == 0 and s["answer_present"][0] == 0 and "tool crashed" in s["not_failed"][1]


def test_slow_runs_are_flagged_and_unknown_latency_is_skipped():
    assert by_name({**GOOD, "latency": 30})["fast_enough"][0] == 0
    assert "fast_enough" not in by_name({**GOOD, "latency": None})


@pytest.mark.parametrize("text,kind", [("mail jane.roe@example.com", "email"), ("card 4111 1111 1111 1111", "card"),
                                       ("ssn 123-45-6789", "ssn")])
def test_a_raw_pii_pattern_is_reported_as_a_leak(text, kind):
    s = by_name({**GOOD, "input": text})
    assert s["no_pii_leak"][0] == 0 and kind in s["no_pii_leak"][1]


def test_redaction_labels_are_not_mistaken_for_leaks():
    assert q.find_leaks("[REDACTED_EMAIL] [REDACTED_CARD] [REDACTED_SSN] [REDACTED_PHONE]") == []


def test_structured_input_is_checked_too():
    assert q.find_leaks([{"role": "user", "content": "I am a@b.co"}]) == ["email"]


def test_score_requests_have_stable_ids_so_reruns_update_instead_of_duplicate():
    a, b = q.score_requests(GOOD, 10), q.score_requests(GOOD, 10)
    assert [r["id"] for r in a] == [r["id"] for r in b] and len({r["id"] for r in a}) == len(a)
    assert all(r["traceId"] == "t-good" and r["dataType"] == "BOOLEAN" for r in a)


def test_model_price_file_is_valid():
    models = json.loads(q.PRICES.read_text())["models"]
    assert models
    for m in models:
        assert m["modelName"] and m["matchPattern"] and m["unit"] == "TOKENS"
        assert m["inputPrice"] >= 0 and m["outputPrice"] >= 0
        import re
        re.compile(m["matchPattern"].replace("(?i)", "", 1))   # the regex must be valid


# ---- against a fake Langfuse ----
class FakeLangfuse:
    def __init__(self, traces, models=()):
        self.traces, self.models, self.scores, self.auth, self.created = traces, list(models), [], set(), []
        self.queries = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, body, code=200):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.auth.add(self.headers.get("Authorization", "")[:6])
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/api/public/v2/observations":
                    outer.queries.append(query)
                    start = int(query.get("cursor", ["0"])[0])
                    rows = []
                    for t in outer.traces:                                   # one top-level row + one child per trace
                        rows.append({"traceId": t["id"], "name": t["name"], "parentObservationId": None,
                                     "input": t["input"], "output": t["output"], "latency": t["latency"]})
                        rows.append({"traceId": t["id"], "name": "child", "parentObservationId": "root",
                                     "input": "x", "output": "mail child@leak.io", "latency": 99})
                    page = rows[start:start + 3]                             # 3 per page: exercises cursor paging
                    meta = {"cursor": str(start + 3)} if start + 3 < len(rows) else {}
                    self._json({"data": page, "meta": meta})
                elif url.path == "/api/public/models":
                    self._json({"data": [{"modelName": n} for n in outer.models], "meta": {"page": 1, "totalPages": 1}})
                else:
                    self._json({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/public/scores":
                    outer.scores.append(body)
                elif self.path == "/api/public/models":
                    outer.created.append(body)
                else:
                    return self._json({}, 404)
                self._json({"id": "x"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")


def test_score_dry_run_writes_nothing_and_apply_writes_everything(env, capsys):
    traces = [{**GOOD, "id": f"t{i}"} for i in range(5)] + [{**GOOD, "id": "leaky", "output": "mail a.b@c.io"}]
    fake = FakeLangfuse(traces)
    try:
        assert q.main(["--host", fake.url, "score"]) == 0
        assert fake.scores == [] and "Dry run" in capsys.readouterr().out
        assert q.main(["--host", fake.url, "score", "--apply"]) == 0
        out = capsys.readouterr().out
    finally:
        fake.stop()
    assert len(fake.scores) == 6 * 4                       # paged through all 6 traces, 4 scores each
    assert "no_pii_leak" in out and "leaky"[:12] in out     # the leak was reported
    assert all("fromStartTime" in x and "toStartTime" in x for x in fake.queries)   # v2 requires a time window
    assert fake.auth == {"Basic "}                          # authenticated with the key pair


def test_models_creates_only_the_missing_ones(env, capsys):
    names = [m["modelName"] for m in json.loads(q.PRICES.read_text())["models"]]
    fake = FakeLangfuse([], models=[names[0]])
    try:
        assert q.main(["--host", fake.url, "models"]) == 0
        assert fake.created == []
        assert q.main(["--host", fake.url, "models", "--apply"]) == 0
    finally:
        fake.stop()
    assert sorted(m["modelName"] for m in fake.created) == sorted(names[1:])


def test_missing_secret_gives_a_clear_message(monkeypatch):
    for k in ("LANGFUSE_SECRET_KEY", "LANGFUSE_INIT_PROJECT_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(q, "load_dotenv", lambda p: None)
    with pytest.raises(SystemExit, match="secret key"):
        q.main(["score"])


def test_unreachable_langfuse_gives_a_clear_message(env):
    with pytest.raises(SystemExit, match="port-forward"):
        q.main(["--host", "http://127.0.0.1:1", "score"])


def test_helpers_read_v2_rows():
    assert q._text('"hello"') == "hello" and q._text(None) == ""
    row = {"traceId": "T", "name": "n", "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-01T00:00:02.5Z"}
    assert q.to_trace(row)["latency"] == 2.5 and q.to_trace(row)["id"] == "T"
