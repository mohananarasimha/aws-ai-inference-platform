"""
scripts/run_onprem_load_test.py: argument parsing, prompt construction, statistics, and the
stage accounting against a stub server on localhost (two tiny stages; no real target).
"""

import csv
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_onprem_load_test as lt  # noqa: E402


def test_defaults_match_the_experiment_design():
    args = lt.parse_args([])
    assert args.stages == [1, 10, 25, 50, 75, 100, 125, 150]
    assert (args.warmup_seconds, args.measurement_seconds, args.max_tokens) == (20, 60, 128)
    assert args.target.describe() == "http://10.100.0.3:18082/v1/chat/completions"


def test_stages_and_validation():
    assert lt.parse_args(["--stages", "1,5, 9"]).stages == [1, 5, 9]
    for bad in (["--stages", "0,5"], ["--stages", "a"], ["--measurement-seconds", "0"],
                ["--stop-error-rate", "2"], ["--url", "ftp://x"], ["--prompt-words", "5"]):
        with pytest.raises(SystemExit):
            lt.parse_args(bad)


def test_url_with_path_is_not_doubled():
    t = lt.Target("http://h:1/v1/chat/completions", 5)
    assert t.path == "/v1/chat/completions"
    assert lt.Target("http://h:1", 5).path == "/v1/chat/completions"


def test_dry_run_sends_nothing_and_writes_nothing(tmp_path, capsys):
    assert lt.main(["--dry-run", "--output-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "10.100.0.3:18082" in out and "sample body" in out
    assert not (tmp_path / "out").exists()


def test_prompts_are_unique_first_and_constant_size():
    a = lt.build_prompt(lt.request_id("abcd1234", 1, 1), 250)
    b = lt.build_prompt(lt.request_id("abcd1234", 1, 2), 250)
    assert a != b and len(a) == len(b)
    assert a.startswith("Request ID: abcd1234-s001-0000001-")
    # the unique part comes first, so no long shared prefix can be cached
    assert a[:60] != b[:60]
    body = json.loads(lt.build_payload("m", "id", 250, 128))
    assert body["temperature"] == 0 and body["max_tokens"] == 128 and body["stream"] is False


def test_percentile_interpolates():
    values = [10.0, 20.0, 30.0, 40.0]
    assert lt.percentile(values, 50) == 25.0
    assert lt.percentile(values, 100) == 40.0
    assert lt.percentile([7.0], 99) == 7.0
    assert lt.percentile([], 50) is None


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                           "usage": {"prompt_tokens": 300, "completion_tokens": 128}}
                          if self.status == 200 else {"error": "boom"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    servers = []

    def start(status):
        handler = type("H", (Stub,), {"status": status})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for s in servers:
        s.shutdown()


def run(tmp_path, url, extra=()):
    out = tmp_path / "out"
    code = lt.main(["--url", url, "--output-dir", str(out), "--warmup-seconds", "0.2",
                    "--measurement-seconds", "0.6", "--skip-preflight", *extra])
    with open(out / "load-stage-summary.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return code, rows, json.loads((out / "load-stage-windows.json").read_text()), out


def test_two_stages_against_stub(tmp_path, stub):
    code, rows, windows, out = run(tmp_path, stub(200), ["--stages", "1,3",
                                                         "--per-request-jsonl", str(tmp_path / "req.jsonl")])
    assert code == 0 and windows["status"] == "completed"
    assert [r["concurrency"] for r in rows] == ["1", "3"]
    for r in rows:
        assert int(r["requests_failed"]) == 0 and int(r["requests_successful"]) > 0
        assert int(r["requests_attempted"]) == int(r["requests_successful"])
        assert float(r["requests_per_sec"]) == pytest.approx(int(r["requests_successful"]) / 0.6, abs=0.01)
        assert float(r["mean_prompt_tokens"]) == 300 and float(r["mean_completion_tokens"]) == 128
    w = windows["stages"][0]
    assert w["warmup_start_utc"] < w["measurement_start_utc"] < w["measurement_end_utc"] <= w["drain_end_utc"]
    assert list(rows[0].keys())[:11] == ["stage", "concurrency", "start_utc", "end_utc", "requests_attempted",
                                         "requests_successful", "requests_failed", "requests_per_sec",
                                         "p50_ms", "p95_ms", "p99_ms"]
    phases = {json.loads(line)["phase"] for line in (tmp_path / "req.jsonl").read_text().splitlines()}
    assert "measure" in phases and "warmup" in phases


def test_stop_condition_ends_the_run(tmp_path, stub):
    code, rows, windows, _ = run(tmp_path, stub(500), ["--stages", "2,4,6", "--stop-error-rate", "0.5"])
    assert code == 0 and windows["status"] == "stopped_error_rate"
    assert len(rows) == 1 and float(rows[0]["error_rate"]) == 1.0
    assert rows[0]["requests_per_sec"] == "0.0" and windows["stages"][0]["errors"] == {"http_500": int(rows[0]["requests_failed"])}


def test_refuses_to_overwrite_without_flag(tmp_path, stub):
    url = stub(200)
    run(tmp_path, url, ["--stages", "1"])
    with pytest.raises(SystemExit):
        run(tmp_path, url, ["--stages", "1"])
    run(tmp_path, url, ["--stages", "1", "--overwrite"])
