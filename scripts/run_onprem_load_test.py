#!/usr/bin/env python3
"""
Step-load test for the on-prem vLLM serving layer. Run it from the AWS router.

  load generator -> Nginx least_conn (10.100.0.3:18082) -> two vLLM replicas -> GPUs

It talks to the Nginx endpoint directly. It never goes through API Gateway,
Cognito or Lambda, so the numbers describe inference-serving capacity only.
Standard library only (threads + http.client): nothing to install on the router.

The load is closed-loop: a stage with concurrency N keeps exactly N requests in
flight, each worker sending its next request as soon as the previous one ends.
Stages run one after another. Each stage has a warm-up (results discarded) and
a measurement window, then the in-flight requests drain before the next stage.

What a stage counts (so the numbers are not open to interpretation):
  - A request belongs to the measurement window if it COMPLETES inside it.
  - successful / failed / attempted are counted over those completions;
    attempted = successful + failed.
  - requests_per_sec = successful / measurement_seconds (failures are not throughput).
  - p50/p95/p99 are over successful requests only, measured client side from
    sending the request to reading the whole response body.
  - unfinished_at_end = requests still in flight when the window closed; they are
    drained and excluded, and reported so the cut-off is visible.
With few completions (low concurrency, long outputs) p99 is close to the maximum;
read it together with the "successful" column.

Outputs (in --output-dir, default ./onprem-load-results):
  load-stage-summary.csv   one row per completed stage
  load-stage-windows.json  run settings and the UTC window of every stage, used by
                           `onprem_metrics.py summarize` to line up GPU/vLLM metrics
  --per-request-jsonl PATH optional, one line per request; large, keep it local

  scripts/run_onprem_load_test.py --dry-run            show the plan, send nothing
  scripts/run_onprem_load_test.py                      run the default stages
  scripts/run_onprem_load_test.py --stages 1,10,25 --measurement-seconds 30

The router must have a synchronised clock (chrony/timedatectl): the stage windows
are matched to the GPU host's samples by UTC timestamp.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import socket
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URL = "http://10.100.0.3:18082"
CHAT_PATH = "/v1/chat/completions"
DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_STAGES = [1, 10, 25, 50, 75, 100, 125, 150]

SUMMARY_FIELDS = [
    "stage", "concurrency", "start_utc", "end_utc",
    "requests_attempted", "requests_successful", "requests_failed",
    "requests_per_sec", "p50_ms", "p95_ms", "p99_ms",
    "error_rate", "mean_ms", "max_ms", "unfinished_at_end",
    "mean_prompt_tokens", "mean_completion_tokens", "completion_tokens_per_sec",
    "generator_cpu_pct",
]

# Deterministic filler so every prompt has the same size. Only the request ID at
# the front differs; because it comes first, no two prompts share a cacheable prefix.
_NOTES = (
    "The inference service runs two replicas behind a least-connections load balancer. "
    "Each replica serves a seven billion parameter model on one GPU with a fixed KV cache budget. "
    "Operators watch queue depth, cache utilization, preemptions, token throughput and tail latency. "
    "A request waits in the queue when every batch slot is taken, and it is preempted when the cache is full. "
    "Network latency between the load generator and the service is small and stable. "
    "Describe which signal would show saturation first and why."
).split()
_TASK = (
    "Read the infrastructure notes below and write a short summary of the likely bottleneck, "
    "the evidence for it and the next three checks an operator should run. "
)


class Interrupted(Exception):
    pass


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100) of an already sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def parse_stages(text: str) -> list[int]:
    try:
        stages = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"stages must be comma separated integers, got {text!r}")
    if not stages or any(s < 1 for s in stages):
        raise argparse.ArgumentTypeError("stages must be one or more integers >= 1")
    return stages


def request_id(run_id: str, stage: int, n: int) -> str:
    return f"{run_id}-s{stage:03d}-{n:07d}-{uuid.uuid4().hex[:12]}"


def build_prompt(req_id: str, prompt_words: int) -> str:
    body = " ".join(_NOTES[i % len(_NOTES)] for i in range(prompt_words))
    return f"Request ID: {req_id}. {_TASK}Notes: {body}"


def build_payload(model: str, req_id: str, prompt_words: int, max_tokens: int) -> bytes:
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(req_id, prompt_words)}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()


class Target:
    def __init__(self, url: str, timeout: float):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"--url must look like http://host:port, got {url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        base = parts.path.rstrip("/")
        self.path = base if base.endswith(CHAT_PATH) else base + CHAT_PATH
        self.timeout = timeout

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def describe(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"


def do_request(conn_box: list, target: Target, body: bytes) -> dict:
    """One request on a worker's persistent connection. Never raises."""
    result = {"ok": False, "status": None, "error": None, "prompt_tokens": None, "completion_tokens": None}
    try:
        if conn_box[0] is None:
            conn_box[0] = target.connect()
        conn_box[0].request("POST", target.path, body=body,
                            headers={"Content-Type": "application/json", "Accept": "application/json"})
        resp = conn_box[0].getresponse()
        raw = resp.read()
        result["status"] = resp.status
        if resp.status != 200:
            result["error"] = f"http_{resp.status}"
            return result
        try:
            data = json.loads(raw)
        except ValueError:
            result["error"] = "bad_json"
            return result
        if not data.get("choices"):
            result["error"] = "no_choices"
            return result
        usage = data.get("usage") or {}
        result["prompt_tokens"] = usage.get("prompt_tokens")
        result["completion_tokens"] = usage.get("completion_tokens")
        result["ok"] = True
        return result
    except socket.timeout:
        result["error"] = "timeout"
    except (OSError, http.client.HTTPException) as exc:
        result["error"] = type(exc).__name__
    try:
        conn_box[0].close()
    except Exception:
        pass
    conn_box[0] = None
    return result


class Stage:
    """Shared state for the workers of one stage."""

    def __init__(self, args, target, run_id, stage, concurrency, jsonl):
        self.args, self.target, self.run_id = args, target, run_id
        self.stage, self.concurrency, self.jsonl = stage, concurrency, jsonl
        self.stop_issuing = threading.Event()
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.counter = 0

    def next_id(self) -> int:
        with self.lock:
            self.counter += 1
            return self.counter

    def worker(self) -> None:
        conn_box = [None]
        while not self.stop_issuing.is_set():
            req_id = request_id(self.run_id, self.stage, self.next_id())
            body = build_payload(self.args.model, req_id, self.args.prompt_words, self.args.max_tokens)
            t0_wall, t0 = time.time(), time.perf_counter()
            res = do_request(conn_box, self.target, body)
            res.update(request_id=req_id, t_start=t0_wall, t_end=time.time(),
                       latency_ms=(time.perf_counter() - t0) * 1000.0)
            with self.lock:
                self.records.append(res)
        if conn_box[0] is not None:
            try:
                conn_box[0].close()
            except Exception:
                pass


def sleep_until(deadline: float, interrupt: threading.Event) -> None:
    remaining = deadline - time.time()
    if remaining > 0 and interrupt.wait(remaining):
        raise Interrupted()


def run_stage(args, target, run_id, index, total, concurrency, jsonl, interrupt):
    stage = Stage(args, target, run_id, index, concurrency, jsonl)
    t_start = time.time()
    t_meas_start = t_start + args.warmup_seconds
    t_meas_end = t_meas_start + args.measurement_seconds
    threads = [threading.Thread(target=stage.worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    try:
        sleep_until(t_meas_start, interrupt)
        cpu0, wall0 = time.process_time(), time.perf_counter()
        sleep_until(t_meas_end, interrupt)
        cpu1, wall1 = time.process_time(), time.perf_counter()
    except (Interrupted, KeyboardInterrupt):
        stage.stop_issuing.set()
        raise Interrupted()
    stage.stop_issuing.set()
    for t in threads:
        t.join(timeout=args.request_timeout + 5)
    t_drain_end = time.time()

    with stage.lock:
        records = list(stage.records)
    in_window = [r for r in records if t_meas_start <= r["t_end"] < t_meas_end]
    ok = [r for r in in_window if r["ok"]]
    latencies = sorted(r["latency_ms"] for r in ok)
    attempted = len(in_window)
    failed = attempted - len(ok)
    window = args.measurement_seconds
    mean_of = lambda key: (sum(r[key] for r in ok if r[key] is not None) /
                           max(1, sum(1 for r in ok if r[key] is not None))) if ok else None
    comp_total = sum(r["completion_tokens"] or 0 for r in ok)
    row = {
        "stage": index, "concurrency": concurrency,
        "start_utc": utc_iso(t_meas_start), "end_utc": utc_iso(t_meas_end),
        "requests_attempted": attempted, "requests_successful": len(ok), "requests_failed": failed,
        "requests_per_sec": round(len(ok) / window, 3),
        "p50_ms": _r(percentile(latencies, 50)), "p95_ms": _r(percentile(latencies, 95)),
        "p99_ms": _r(percentile(latencies, 99)),
        "error_rate": round(failed / attempted, 4) if attempted else 1.0,
        "mean_ms": _r(sum(latencies) / len(latencies)) if latencies else None,
        "max_ms": _r(latencies[-1]) if latencies else None,
        "unfinished_at_end": sum(1 for r in records if r["t_end"] >= t_meas_end),
        "mean_prompt_tokens": _r(mean_of("prompt_tokens")),
        "mean_completion_tokens": _r(mean_of("completion_tokens")),
        "completion_tokens_per_sec": round(comp_total / window, 2),
        "generator_cpu_pct": round((cpu1 - cpu0) / max(wall1 - wall0, 1e-9) * 100.0, 1),
    }
    detail = {
        "stage": index, "concurrency": concurrency,
        "warmup_start_utc": utc_iso(t_start),
        "measurement_start_utc": utc_iso(t_meas_start),
        "measurement_end_utc": utc_iso(t_meas_end),
        "drain_end_utc": utc_iso(t_drain_end),
        "errors": dict(Counter(r["error"] for r in in_window if not r["ok"])),
        "loadavg_1m_at_end": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
    }
    if jsonl is not None:
        for r in records:
            phase = ("warmup" if r["t_end"] < t_meas_start
                     else "measure" if r["t_end"] < t_meas_end else "drain")
            jsonl.write(json.dumps({
                "stage": index, "concurrency": concurrency, "phase": phase,
                "request_id": r["request_id"], "start_utc": utc_iso(r["t_start"]),
                "end_utc": utc_iso(r["t_end"]), "latency_ms": _r(r["latency_ms"]), "ok": r["ok"],
                "status": r["status"], "error": r["error"],
                "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
            }) + "\n")
        jsonl.flush()
    return row, detail


def _r(value):
    return None if value is None else round(value, 1)


def stage_line(row: dict, index: int, total: int) -> str:
    def ms(v):
        return "-" if v is None else f"{v:.0f}ms"
    line = (f"stage {index}/{total} conc={row['concurrency']:<4} ok={row['requests_successful']:<5} "
            f"fail={row['requests_failed']:<4} rps={row['requests_per_sec']:<7} "
            f"p50={ms(row['p50_ms'])} p95={ms(row['p95_ms'])} p99={ms(row['p99_ms'])} "
            f"cpu={row['generator_cpu_pct']:.0f}%")
    if row["generator_cpu_pct"] >= 80:
        line += "  WARNING: load generator is busy, results may be generator-limited"
    return line


def write_windows(path: Path, meta: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(tmp, path)


def preflight(target: Target, args) -> None:
    box = [None]
    body = build_payload(args.model, request_id("preflight", 0, 0), args.prompt_words, 8)
    t0 = time.perf_counter()
    res = do_request(box, target, body)
    ms = (time.perf_counter() - t0) * 1000
    if not res["ok"]:
        raise SystemExit(f"preflight failed against {target.describe()}: "
                         f"{res['error']} (status {res['status']}). Nothing was measured.")
    print(f"preflight ok: {ms:.0f} ms, prompt_tokens={res['prompt_tokens']}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL, help=f"Nginx endpoint (default {DEFAULT_URL})")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--stages", type=parse_stages, default=DEFAULT_STAGES,
                   help="comma separated concurrency levels (default 1,10,25,50,75,100,125,150)")
    p.add_argument("--warmup-seconds", type=float, default=20)
    p.add_argument("--measurement-seconds", type=float, default=60)
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--prompt-words", type=int, default=250,
                   help="words of filler per prompt; keeps prompt size constant (default 250)")
    p.add_argument("--request-timeout", type=float, default=120,
                   help="client timeout per request in seconds (default 120)")
    p.add_argument("--stop-error-rate", type=float, default=0.5,
                   help="stop before the next stage if a stage's error rate reaches this "
                        "fraction; 0 disables (default 0.5)")
    p.add_argument("--output-dir", default="onprem-load-results")
    p.add_argument("--per-request-jsonl", metavar="PATH", default=None,
                   help="also write one JSON line per request (large; keep it local)")
    p.add_argument("--overwrite", action="store_true", help="replace existing output files")
    p.add_argument("--skip-preflight", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print the plan and a sample request; send nothing")
    args = p.parse_args(argv)
    if args.warmup_seconds < 0 or args.measurement_seconds <= 0:
        p.error("--warmup-seconds must be >= 0 and --measurement-seconds > 0")
    if args.max_tokens < 1 or args.prompt_words < 20 or args.request_timeout <= 0:
        p.error("--max-tokens >= 1, --prompt-words >= 20 and --request-timeout > 0 are required")
    if not 0 <= args.stop_error_rate <= 1:
        p.error("--stop-error-rate must be between 0 and 1")
    try:
        args.target = Target(args.url, args.request_timeout)
    except ValueError as exc:
        p.error(str(exc))
    return args


def print_plan(args) -> None:
    per_stage = args.warmup_seconds + args.measurement_seconds
    sample = build_payload(args.model, request_id("DRYRUN", 1, 1), args.prompt_words, args.max_tokens)
    print(f"target:       {args.target.describe()}")
    print(f"model:        {args.model}")
    print(f"stages:       {', '.join(map(str, args.stages))}")
    print(f"per stage:    {args.warmup_seconds:g}s warm-up + {args.measurement_seconds:g}s measurement, "
          f"then drain (up to {args.request_timeout:g}s)")
    print(f"minimum run:  about {len(args.stages) * per_stage / 60:.1f} minutes")
    print(f"stop rule:    error rate >= {args.stop_error_rate:g} ends the run"
          if args.stop_error_rate else "stop rule:    none")
    print(f"output dir:   {args.output_dir}")
    print(f"sample body ({len(sample)} bytes):")
    print(json.dumps(json.loads(sample), indent=2)[:900] + " ...")


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        print_plan(args)
        return 0

    out = Path(args.output_dir)
    summary_path, windows_path = out / "load-stage-summary.csv", out / "load-stage-windows.json"
    jsonl_path = Path(args.per_request_jsonl) if args.per_request_jsonl else None
    existing = [p for p in (summary_path, windows_path, jsonl_path) if p is not None and p.exists()]
    if existing and not args.overwrite:
        raise SystemExit("refusing to overwrite: " + ", ".join(map(str, existing)) +
                         " (use --overwrite or another --output-dir)")
    out.mkdir(parents=True, exist_ok=True)

    if not args.skip_preflight:
        preflight(args.target, args)

    run_id = uuid.uuid4().hex[:8]
    meta = {
        "tool": "scripts/run_onprem_load_test.py", "run_id": run_id,
        "target": args.target.describe(), "model": args.model,
        "config": {
            "stages": args.stages, "warmup_seconds": args.warmup_seconds,
            "measurement_seconds": args.measurement_seconds, "max_tokens": args.max_tokens,
            "prompt_words": args.prompt_words, "temperature": 0, "stream": False,
            "request_timeout": args.request_timeout, "stop_error_rate": args.stop_error_rate,
        },
        "started_utc": utc_iso(time.time()), "finished_utc": None, "status": "running", "stages": [],
    }
    interrupt = threading.Event()
    jsonl = open(jsonl_path, "w") if jsonl_path else None
    status = "completed"
    print(f"run {run_id}: {args.target.describe()}  stages={args.stages}")
    try:
        with open(summary_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
            writer.writeheader()
            fh.flush()
            for i, conc in enumerate(args.stages, start=1):
                print(f"stage {i}/{len(args.stages)} conc={conc}: warm-up {args.warmup_seconds:g}s, "
                      f"measuring {args.measurement_seconds:g}s ...", flush=True)
                row, detail = run_stage(args, args.target, run_id, i, len(args.stages), conc, jsonl, interrupt)
                writer.writerow(row)
                fh.flush()
                meta["stages"].append(detail)
                write_windows(windows_path, meta)
                print(stage_line(row, i, len(args.stages)), flush=True)
                if args.stop_error_rate and row["error_rate"] >= args.stop_error_rate and i < len(args.stages):
                    status = "stopped_error_rate"
                    print(f"stopping: error rate {row['error_rate']:.2%} >= {args.stop_error_rate:.2%} "
                          f"at concurrency {conc}")
                    break
    except (Interrupted, KeyboardInterrupt):
        status = "interrupted"
        print("\ninterrupted: the unfinished stage is discarded; completed stages are saved")
    finally:
        meta["status"] = status
        meta["finished_utc"] = utc_iso(time.time())
        write_windows(windows_path, meta)
        if jsonl is not None:
            jsonl.close()
    print(f"status: {status}\nwrote {summary_path}\nwrote {windows_path}")
    return 130 if status == "interrupted" else 0


if __name__ == "__main__":
    sys.exit(main())
