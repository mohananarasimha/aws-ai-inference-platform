#!/usr/bin/env python3
"""
Capture vLLM + GPU metrics during the on-prem load test, then line them up with
the load-stage windows. Standard library only.

  capture    run on the office GPU host while the load test runs on the router
  summarize  run afterwards, offline, on the three files from the two machines

  scripts/onprem_metrics.py capture --list-metrics          what the replicas expose
  scripts/onprem_metrics.py capture                         sample once per second until Ctrl+C
  scripts/onprem_metrics.py summarize                       -> onprem-load-summary.csv

vLLM metric names. They come from vllm/v1/metrics/loggers.py at tag v0.33.0 and
are used exactly as defined there; none are invented. prometheus_client adds
"_total" to counters when exposing them, so a counter is looked up under both
spellings and the first one found is used. Always confirm against the live
endpoint: `capture` writes <output>.metric-names.txt with the names each replica
really exposes and warns about any expected metric that is missing.

  vllm:num_requests_running               gauge      requests in the current batch
  vllm:num_requests_waiting               gauge      requests queued
  vllm:kv_cache_usage_perc                gauge      KV cache usage, 1 = 100%
  vllm:num_preemptions[_total]            counter    cumulative preemptions
  vllm:prompt_tokens[_total]              counter    prefill tokens
  vllm:generation_tokens[_total]          counter    generated tokens
  vllm:time_to_first_token_seconds        histogram  TTFT
  vllm:e2e_request_latency_seconds        histogram  server-side end-to-end latency
  vllm:request_time_per_output_token_seconds  histogram  mean time per output token, per request
  vllm:inter_token_latency_seconds        histogram  gap between consecutive output tokens

GPU numbers come from one `nvidia-smi --query-gpu=... --format=csv` call per sample.

Every row of the time series has a UTC timestamp. The router and the GPU host are
different machines: check both clocks are synchronised (chrony/timedatectl). If
they are not, pass --metrics-clock-offset-seconds to `summarize`.

Histogram percentiles (TTFT, TPOT, ...) are estimated from the cumulative buckets
by linear interpolation, so they are only as fine as vLLM's bucket edges.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPLICAS = ["gpu0=http://10.100.0.3:18080/metrics", "gpu1=http://10.100.0.3:18081/metrics"]
NVIDIA_QUERY = "index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

# column -> (candidate metric names in order, how to combine label sets)
SCALARS = {
    "running": (["vllm:num_requests_running"], "sum"),
    "waiting": (["vllm:num_requests_waiting"], "sum"),
    "kv_cache_usage": (["vllm:kv_cache_usage_perc"], "max"),
    "preemptions_total": (["vllm:num_preemptions_total", "vllm:num_preemptions"], "sum"),
    "prompt_tokens_total": (["vllm:prompt_tokens_total", "vllm:prompt_tokens"], "sum"),
    "generation_tokens_total": (["vllm:generation_tokens_total", "vllm:generation_tokens"], "sum"),
}
HISTOGRAMS = {
    "ttft": "vllm:time_to_first_token_seconds",
    "e2e": "vllm:e2e_request_latency_seconds",
    "tpot": "vllm:request_time_per_output_token_seconds",
    "itl": "vllm:inter_token_latency_seconds",
}
NV_FIELDS = ["util_pct", "mem_used_mib", "mem_total_mib", "temp_c", "power_w"]


# ----------------------------------------------------------------- prometheus text

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> list[tuple[str, dict, float]]:
    """Parse Prometheus text exposition into (name, labels, value). Comments are skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = dict(_LABEL.findall(m.group(2) or ""))
        out.append((m.group(1), labels, value))
    return out


def find_scalar(samples, candidates, how):
    """First candidate name that exists -> (value, name_used); (None, None) when none does."""
    for name in candidates:
        values = [v for n, _, v in samples if n == name and not math.isnan(v)]
        if values:
            return (max(values) if how == "max" else sum(values)), name
    return None, None


def find_histogram(samples, base):
    """Cumulative buckets summed over label sets -> (buckets {le: count}, sum, count) or None."""
    buckets: dict[float, float] = {}
    for n, labels, v in samples:
        if n == base + "_bucket" and "le" in labels:
            le = float(labels["le"].replace("+Inf", "inf"))
            buckets[le] = buckets.get(le, 0.0) + v
    if not buckets:
        return None
    total_sum = sum(v for n, _, v in samples if n == base + "_sum")
    total_count = sum(v for n, _, v in samples if n == base + "_count")
    return buckets, total_sum, total_count


def encode_buckets(buckets: dict[float, float]) -> str:
    return ",".join(f"{'+Inf' if math.isinf(le) else repr(le)}:{count:g}"
                    for le, count in sorted(buckets.items()))


def decode_buckets(text: str) -> dict[float, float]:
    out = {}
    for part in text.split(","):
        le, _, count = part.rpartition(":")
        out[float(le.replace("+Inf", "inf"))] = float(count)
    return out


def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """Prometheus-style quantile from cumulative buckets. A rank beyond the last
    finite edge returns that edge (the true value is at least that large)."""
    if not buckets:
        return None
    edges = sorted(buckets)
    total = buckets[edges[-1]]
    if total <= 0:
        return None
    rank = q * total
    prev_edge, prev_count = 0.0, 0.0
    for edge in edges:
        count = buckets[edge]
        if count >= rank:
            if math.isinf(edge):
                return prev_edge
            if count == prev_count:
                return edge
            return prev_edge + (edge - prev_edge) * (rank - prev_count) / (count - prev_count)
        prev_edge, prev_count = edge, count
    return prev_edge


def metric_inventory(samples) -> dict[str, str]:
    """Distinct vllm: metric families present -> 'histogram' or 'series'."""
    families: dict[str, str] = {}
    for name, _, _ in samples:
        if not name.startswith("vllm:"):
            continue
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix):
                families[name[: -len(suffix)]] = "histogram"
                break
        else:
            families.setdefault(name, "series")
    return families


def expected_metric_report(samples) -> list[str]:
    """Names of expected metrics that this endpoint does not expose."""
    missing = []
    for col, (candidates, how) in SCALARS.items():
        if find_scalar(samples, candidates, how)[1] is None:
            missing.append(" or ".join(candidates))
    for col, base in HISTOGRAMS.items():
        if find_histogram(samples, base) is None:
            missing.append(base)
    return missing


# ----------------------------------------------------------------------- capture


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_utc(text: str) -> float:
    return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()


def scrape(url: str, timeout: float) -> list:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return parse_prometheus(resp.read().decode("utf-8", "replace"))


def parse_nvidia_csv(text: str) -> dict[int, dict]:
    """Parse `--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw`
    with csv,noheader,nounits output. '[N/A]' and other non-numbers become None."""
    gpus = {}
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            continue

        def num(s):
            try:
                return float(s)
            except ValueError:
                return None
        try:
            index = int(parts[0])
        except ValueError:
            continue
        gpus[index] = dict(zip(NV_FIELDS, (num(p) for p in parts[1:])))
    return gpus


def query_nvidia(timeout: float) -> dict[int, dict]:
    out = subprocess.run(["nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=timeout, check=True)
    return parse_nvidia_csv(out.stdout)


def capture_header(replicas: list[str], gpus: list[int]) -> list[str]:
    cols = ["timestamp_utc"]
    for r in replicas:
        cols += [f"vllm_{r}_scrape_ok"] + [f"vllm_{r}_{c}" for c in SCALARS]
        for h in HISTOGRAMS:
            cols += [f"vllm_{r}_{h}_buckets", f"vllm_{r}_{h}_sum", f"vllm_{r}_{h}_count"]
    for g in gpus:
        cols += [f"nvsmi_gpu{g}_{f}" for f in NV_FIELDS]
    return cols


def vllm_columns(replica: str, samples) -> dict:
    row = {f"vllm_{replica}_scrape_ok": 1}
    for col, (candidates, how) in SCALARS.items():
        value, _ = find_scalar(samples, candidates, how)
        row[f"vllm_{replica}_{col}"] = "" if value is None else f"{value:g}"
    for h, base in HISTOGRAMS.items():
        found = find_histogram(samples, base)
        if found:
            buckets, s, c = found
            row[f"vllm_{replica}_{h}_buckets"] = encode_buckets(buckets)
            row[f"vllm_{replica}_{h}_sum"] = f"{s:g}"
            row[f"vllm_{replica}_{h}_count"] = f"{c:g}"
    return row


def write_inventory(path: Path, endpoints: dict[str, str], timeout: float) -> bool:
    lines, all_ok = [], True
    for name, url in endpoints.items():
        lines.append(f"## {name}  {url}")
        try:
            samples = scrape(url, timeout)
        except Exception as exc:
            lines.append(f"scrape failed: {type(exc).__name__}: {exc}\n")
            all_ok = False
            continue
        for family, kind in sorted(metric_inventory(samples).items()):
            lines.append(f"{family}  [{kind}]")
        missing = expected_metric_report(samples)
        lines.append("expected but NOT exposed: " + (", ".join(missing) if missing else "none") + "\n")
        for m in missing:
            print(f"WARNING {name}: expected metric not exposed: {m}", file=sys.stderr)
    path.write_text("\n".join(lines) + "\n")
    return all_ok


def run_capture(args) -> int:
    endpoints = {}
    for spec in (args.replica or DEFAULT_REPLICAS):
        name, sep, url = spec.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9]+", name) or not url.startswith("http"):
            raise SystemExit(f"--replica must look like gpu0=http://host:18080/metrics, got {spec!r}")
        endpoints[name] = url
    gpus = [int(g) for g in args.gpus.split(",") if g.strip()]
    out = Path(args.output)
    inventory = out.with_suffix(".metric-names.txt")

    if args.list_metrics:
        ok = write_inventory(inventory, endpoints, args.scrape_timeout)
        print(inventory.read_text())
        return 0 if ok else 1
    if out.exists() and not args.overwrite:
        raise SystemExit(f"refusing to overwrite {out} (use --overwrite or another --output)")
    if not args.no_inventory:
        write_inventory(inventory, endpoints, args.scrape_timeout)
        print(f"wrote {inventory}")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    header = capture_header(list(endpoints), gpus)
    rows = 0
    deadline = time.time() + args.duration if args.duration else None
    print(f"capturing every {args.interval:g}s to {out}; Ctrl+C to stop")
    with open(out, "w", newline="") as fh, ThreadPoolExecutor(max_workers=len(endpoints) + 1) as pool:
        writer = csv.DictWriter(fh, fieldnames=header, restval="")
        writer.writeheader()
        fh.flush()
        next_tick = time.time()
        while not stop.is_set() and (deadline is None or time.time() < deadline):
            ts = time.time()
            futures = {name: pool.submit(scrape, url, args.scrape_timeout) for name, url in endpoints.items()}
            nv_future = pool.submit(query_nvidia, args.scrape_timeout + 1)
            row = {"timestamp_utc": utc_iso(ts)}
            for name, fut in futures.items():
                try:
                    row.update(vllm_columns(name, fut.result()))
                except Exception:
                    row[f"vllm_{name}_scrape_ok"] = 0
            try:
                nv = nv_future.result()
                for g in gpus:
                    for f in NV_FIELDS:
                        value = nv.get(g, {}).get(f)
                        row[f"nvsmi_gpu{g}_{f}"] = "" if value is None else f"{value:g}"
            except Exception:
                pass
            writer.writerow(row)
            fh.flush()
            rows += 1
            next_tick += args.interval
            if next_tick < time.time():
                next_tick = time.time()
            stop.wait(max(0.0, next_tick - time.time()))
    print(f"stopped: {rows} rows written to {out}")
    return 0


# --------------------------------------------------------------------- summarize


def fnum(text) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def read_timeseries(path: Path, offset: float) -> tuple[list[dict], list[str]]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        header = reader.fieldnames or []
    for r in rows:
        r["_ts"] = parse_utc(r["timestamp_utc"]) + offset
    rows.sort(key=lambda r: r["_ts"])
    return rows, header


def discover(header: list[str]) -> tuple[list[str], list[int]]:
    replicas = [m.group(1) for c in header if (m := re.fullmatch(r"vllm_(.+)_scrape_ok", c))]
    gpus = sorted({int(m.group(1)) for c in header if (m := re.fullmatch(r"nvsmi_gpu(\d+)_util_pct", c))})
    return replicas, gpus


def window_rows(rows: list[dict], start: float, end: float) -> tuple[list[dict], dict | None]:
    inside = [r for r in rows if start <= r["_ts"] <= end]
    before = [r for r in rows if r["_ts"] < start]
    return inside, (before[-1] if before else None)


def col(rows, name) -> list[float]:
    return [v for v in (fnum(r.get(name)) for r in rows) if v is not None]


def counter_rate(base, rows, name) -> tuple[float | None, float | None]:
    """(delta, seconds) of a cumulative counter across the window, including the sample
    just before it so the first second is not lost. A decrease means the server
    restarted, so the new value counts as the increase."""
    seq = ([base] if base else []) + rows
    delta, seconds, prev = 0.0, 0.0, None
    seen = False
    for r in seq:
        v = fnum(r.get(name))
        if v is None:
            continue
        if prev is not None:
            d = v - prev[1]
            delta += d if d >= 0 else v
            seconds += r["_ts"] - prev[0]
            seen = True
        prev = (r["_ts"], v)
    return (delta, seconds) if seen else (None, None)


def histogram_delta(base, rows, replica, hist) -> dict[float, float] | None:
    name = f"vllm_{replica}_{hist}_buckets"
    seq = ([base] if base else []) + rows
    valid = [r for r in seq if r.get(name)]
    if len(valid) < 2:
        return None
    first, last = decode_buckets(valid[0][name]), decode_buckets(valid[-1][name])
    if any(last.get(le, 0) < first.get(le, 0) for le in last):
        return last
    return {le: last[le] - first.get(le, 0.0) for le in last}


def merged_histogram(base, rows, replicas, hist) -> dict[float, float] | None:
    merged: dict[float, float] = {}
    for r in replicas:
        d = histogram_delta(base, rows, r, hist)
        for le, c in (d or {}).items():
            merged[le] = merged.get(le, 0.0) + c
    return merged or None


def hist_mean(base, rows, replicas, hist) -> float | None:
    total_sum = total_count = 0.0
    for r in replicas:
        ds, _ = counter_rate(base, rows, f"vllm_{r}_{hist}_sum")
        dc, _ = counter_rate(base, rows, f"vllm_{r}_{hist}_count")
        if ds is not None and dc:
            total_sum += ds
            total_count += dc
    return total_sum / total_count if total_count else None


def ms(seconds):
    return None if seconds is None else round(seconds * 1000.0, 1)


def stage_metrics(rows, base, replicas, gpus) -> dict:
    out: dict = {"metric_samples": len(rows)}
    for g in gpus:
        util, vram = col(rows, f"nvsmi_gpu{g}_util_pct"), col(rows, f"nvsmi_gpu{g}_mem_used_mib")
        temp, power = col(rows, f"nvsmi_gpu{g}_temp_c"), col(rows, f"nvsmi_gpu{g}_power_w")
        out[f"gpu{g}_util_avg_pct"] = round(sum(util) / len(util), 1) if util else None
        out[f"gpu{g}_util_max_pct"] = max(util) if util else None
        out[f"gpu{g}_vram_max_mib"] = max(vram) if vram else None
        out[f"gpu{g}_temp_max_c"] = max(temp) if temp else None
        out[f"gpu{g}_power_max_w"] = max(power) if power else None
    kv_all = []
    for r in replicas:
        kv = col(rows, f"vllm_{r}_kv_cache_usage")
        out[f"kv_cache_max_{r}"] = round(max(kv), 4) if kv else None
        kv_all += kv
        out[f"running_max_{r}"] = max(col(rows, f"vllm_{r}_running"), default=None)
        out[f"waiting_max_{r}"] = max(col(rows, f"vllm_{r}_waiting"), default=None)
    out["kv_cache_max"] = round(max(kv_all), 4) if kv_all else None

    def total(row, field):
        vals = [fnum(row.get(f"vllm_{r}_{field}")) for r in replicas]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    running = [v for v in (total(r, "running") for r in rows) if v is not None]
    waiting = [v for v in (total(r, "waiting") for r in rows) if v is not None]
    out["running_max_total"] = max(running) if running else None
    out["waiting_max_total"] = max(waiting) if waiting else None
    out["waiting_samples_gt0"] = sum(1 for v in waiting if v > 0) if waiting else None

    def summed(field):
        deltas = [counter_rate(base, rows, f"vllm_{r}_{field}") for r in replicas]
        return [d for d in deltas if d[0] is not None]
    pre = summed("preemptions_total")
    out["preemptions_delta"] = round(sum(d for d, _ in pre), 3) if pre else None
    for field, key in (("prompt_tokens_total", "prompt_tokens_per_sec"),
                       ("generation_tokens_total", "generation_tokens_per_sec")):
        parts = summed(field)
        out[key] = round(sum(d / s for d, s in parts if s), 2) if parts else None
    for hist, key in (("ttft", "ttft_p95_ms"), ("e2e", "e2e_p95_ms"),
                      ("tpot", "tpot_p95_ms"), ("itl", "itl_p95_ms")):
        merged = merged_histogram(base, rows, replicas, hist)
        out[key] = ms(histogram_quantile(0.95, merged)) if merged else None
    out["tpot_mean_ms"] = ms(hist_mean(base, rows, replicas, "tpot"))
    return out


def summarize(stage_rows, windows, timeseries, args) -> tuple[list[dict], list[str]]:
    rows, header = timeseries
    replicas, gpus = discover(header)
    by_stage = {w["stage"]: w for w in windows["stages"]}
    spacing = sorted(b["_ts"] - a["_ts"] for a, b in zip(rows, rows[1:]))
    median_dt = spacing[len(spacing) // 2] if spacing else None
    out, notes = [], []
    for sr in stage_rows:
        stage = int(sr["stage"])
        w = by_stage.get(stage)
        if w is None:
            notes.append(f"stage {stage}: no window in load-stage-windows.json, skipped")
            continue
        if int(w["concurrency"]) != int(sr["concurrency"]):
            raise SystemExit(f"stage {stage}: concurrency differs between summary CSV "
                             f"({sr['concurrency']}) and windows JSON ({w['concurrency']})")
        start, end = parse_utc(w["measurement_start_utc"]), parse_utc(w["measurement_end_utc"])
        inside, base = window_rows(rows, start, end)
        m = stage_metrics(inside, base, replicas, gpus)
        if median_dt and len(inside) < 0.8 * (end - start) / median_dt:
            notes.append(f"stage {stage} (conc {w['concurrency']}): only {len(inside)} metric samples "
                         f"in a {end - start:.0f}s window; capture coverage is incomplete")
        row = {
            "stage": stage, "concurrency": int(sr["concurrency"]),
            "window_start_utc": w["measurement_start_utc"], "window_end_utc": w["measurement_end_utc"],
            "requests_per_sec": fnum(sr.get("requests_per_sec")),
            "successful": sr.get("requests_successful"), "failed": sr.get("requests_failed"),
            "error_rate": fnum(sr.get("error_rate")),
            "p50_ms": fnum(sr.get("p50_ms")), "p95_ms": fnum(sr.get("p95_ms")), "p99_ms": fnum(sr.get("p99_ms")),
            "client_completion_tokens_per_sec": fnum(sr.get("completion_tokens_per_sec")),
            "generator_cpu_pct": fnum(sr.get("generator_cpu_pct")),
        }
        row.update(m)
        out.append(row)
    return out, notes


def first_where(rows, pred):
    return next((r for r in rows if pred(r)), None)


def findings(rows: list[dict], args) -> list[str]:
    def gt(key, thr):
        return lambda r: r.get(key) is not None and r[key] > thr
    lines = []
    waiting = first_where(rows, gt("waiting_max_total", 0))
    lines.append(f"first concurrency with waiting_requests > 0: conc {waiting['concurrency']} "
                 f"(max waiting {waiting['waiting_max_total']:g}, in {waiting['waiting_samples_gt0']} of "
                 f"{waiting['metric_samples']} samples)" if waiting else
                 "first concurrency with waiting_requests > 0: not observed")
    pre = first_where(rows, gt("preemptions_delta", 0))
    lines.append(f"first concurrency with preemptions: conc {pre['concurrency']} "
                 f"(+{pre['preemptions_delta']:g} in the window)" if pre else
                 "first concurrency with preemptions: not observed")
    kv = first_where(rows, lambda r: r.get("kv_cache_max") is not None and r["kv_cache_max"] >= args.kv_high)
    kv_peak = max((r for r in rows if r.get("kv_cache_max") is not None),
                  key=lambda r: r["kv_cache_max"], default=None)
    if kv:
        lines.append(f"first concurrency with KV cache >= {args.kv_high:.0%}: conc {kv['concurrency']} "
                     f"(max {kv['kv_cache_max']:.1%})")
    else:
        peak = f"; highest seen {kv_peak['kv_cache_max']:.1%} at conc {kv_peak['concurrency']}" if kv_peak else ""
        lines.append(f"first concurrency with KV cache >= {args.kv_high:.0%}: not observed{peak}")
    with_rps = [r for r in rows if r.get("requests_per_sec") is not None]
    if with_rps:
        top = max(with_rps, key=lambda r: r["requests_per_sec"])
        gen = f", generation {top['generation_tokens_per_sec']:g} tok/s" if top.get("generation_tokens_per_sec") else ""
        lines.append(f"peak throughput: {top['requests_per_sec']:g} req/s at conc {top['concurrency']}{gen}")
    stall = None
    for prev, cur in zip(with_rps, with_rps[1:]):
        if (prev["requests_per_sec"] and cur["requests_per_sec"] < prev["requests_per_sec"] * (1 + args.scale_gain_min)
                and prev.get("p95_ms") and cur.get("p95_ms")
                and cur["p95_ms"] > prev["p95_ms"] * (1 + args.p95_rise_min)):
            stall = (prev, cur)
            break
    if stall:
        prev, cur = stall
        lines.append(
            f"first stage where throughput stops scaling while p95 rises: conc {cur['concurrency']} "
            f"(req/s {prev['requests_per_sec']:g} -> {cur['requests_per_sec']:g}, "
            f"p95 {prev['p95_ms']:g} -> {cur['p95_ms']:g} ms; rule: gain < {args.scale_gain_min:.0%} "
            f"and p95 rise > {args.p95_rise_min:.0%} vs the previous stage)")
    else:
        lines.append("throughput stall with rising p95: not observed with the rule "
                     f"(gain < {args.scale_gain_min:.0%} and p95 rise > {args.p95_rise_min:.0%})")
    best = None
    for r in rows:
        for key in (k for k in r if re.fullmatch(r"gpu\d+_util_max_pct", k)):
            if r[key] is not None and (best is None or r[key] > best[0]):
                best = (r[key], key.split("_")[0], r["concurrency"])
    lines.append(f"peak GPU utilization: {best[0]:g}% on {best[1]} at conc {best[2]}" if best
                 else "peak GPU utilization: no data")
    return lines


def run_summarize(args) -> int:
    with open(args.stage_summary, newline="") as fh:
        stage_rows = list(csv.DictReader(fh))
    windows = json.loads(Path(args.windows).read_text())
    timeseries = read_timeseries(Path(args.timeseries), args.metrics_clock_offset_seconds)
    if not timeseries[0]:
        raise SystemExit(f"{args.timeseries} has no rows")
    rows, notes = summarize(stage_rows, windows, timeseries, args)
    if not rows:
        raise SystemExit("no stages could be matched; nothing written")
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(args.output, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, restval="")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    print(f"wrote {args.output}: {len(rows)} stages, run status {windows.get('status')}")
    for note in notes:
        print(f"NOTE {note}", file=sys.stderr)
    print("\nEvidence (heuristics are printed with their thresholds; interpretation is yours):")
    for line in findings(rows, args):
        print(f"  - {line}")
    return 0


# --------------------------------------------------------------------------- cli


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("capture", help="sample vLLM /metrics and nvidia-smi until Ctrl+C")
    c.add_argument("--replica", action="append", metavar="NAME=URL",
                   help="repeatable; default gpu0=http://10.100.0.3:18080/metrics and gpu1=...:18081/metrics")
    c.add_argument("--gpus", default="0,1", help="nvidia-smi GPU indexes to record (default 0,1)")
    c.add_argument("--interval", type=float, default=1.0)
    c.add_argument("--scrape-timeout", type=float, default=2.0)
    c.add_argument("--duration", type=float, default=0, help="stop by itself after N seconds (default: until Ctrl+C)")
    c.add_argument("--output", default="onprem-load-timeseries.csv")
    c.add_argument("--overwrite", action="store_true")
    c.add_argument("--list-metrics", action="store_true",
                   help="scrape once, write and print the metric names each replica exposes, then exit")
    c.add_argument("--no-inventory", action="store_true")
    s = sub.add_parser("summarize", help="correlate load stages with the captured time series (offline)")
    s.add_argument("--stage-summary", default="load-stage-summary.csv")
    s.add_argument("--windows", default="load-stage-windows.json")
    s.add_argument("--timeseries", default="onprem-load-timeseries.csv")
    s.add_argument("--output", default="onprem-load-summary.csv")
    s.add_argument("--kv-high", type=float, default=0.90, help="KV cache fraction counted as high pressure")
    s.add_argument("--scale-gain-min", type=float, default=0.10,
                   help="throughput gain below this vs the previous stage counts as 'not scaling'")
    s.add_argument("--p95-rise-min", type=float, default=0.10,
                   help="p95 rise above this vs the previous stage counts as 'latency rising'")
    s.add_argument("--metrics-clock-offset-seconds", type=float, default=0.0,
                   help="seconds to ADD to the GPU host's timestamps to match the load generator's clock")
    args = p.parse_args(argv)
    if args.mode == "capture" and args.interval <= 0:
        p.error("--interval must be > 0")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    return run_capture(args) if args.mode == "capture" else run_summarize(args)


if __name__ == "__main__":
    sys.exit(main())
