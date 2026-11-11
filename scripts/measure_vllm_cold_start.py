#!/usr/bin/env python3
"""
Measure where vLLM cold-start time goes on EKS, from Kubernetes' own timestamps.

Read-only: it never applies, scales or deletes anything. Start it before (or
after) you create the vLLM Deployment; every milestone except the /health and
inference checks comes from a timestamp stored on a Kubernetes object, so it is
exact whenever the script happens to look, and it also works after the fact
while the pod, NodeClaim and node still exist.

  T0 pod Pending      pod metadata.creationTimestamp
  T1 NodeClaim made   NodeClaim metadata.creationTimestamp
  T2 node Ready       node condition Ready=True lastTransitionTime
  T3 container start  containerStatuses[vllm].state.running.startedAt
  T4 vLLM ready       later of: pod condition Ready=True lastTransitionTime (T4a)
                      and the first successful /health through a port-forward (T4b)
  T5 first inference  one request through API Gateway (--test-inference)

  scripts/measure_vllm_cold_start.py                    watch or analyse the vLLM pod
  scripts/measure_vllm_cold_start.py --test-inference   plus one request at the end
  scripts/measure_vllm_cold_start.py --json             machine-readable result

Kubernetes timestamps have 1 s resolution, so every duration is +/- 1 s.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "default"
POD_LABEL = "app=vllm"
CONTAINER = "vllm"
VLLM_PORT = 8000
DEVICE_PLUGIN_NAMESPACE = "kube-system"
DEVICE_PLUGIN_LABEL = "app.kubernetes.io/name=nvidia-device-plugin"
APP_DIR = Path(__file__).resolve().parent.parent / "terraform" / "environments" / "dev" / "app"

INFERENCE_PROMPT = "Reply with the single word: ready"
FATAL_WAITING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "InvalidImageName", "CreateContainerError", "CreateContainerConfigError"}


class MeasurementError(Exception):
    """A measurement could not be completed; the message says why."""

    def __init__(self, message, partial=None):
        super().__init__(message)
        self.partial = partial


# ---------------------------------------------------------------- time helpers

_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value):
    """RFC 3339 (Kubernetes, kubectl --timestamps) to an aware UTC datetime, or None."""
    if not value:
        return None

    text = value.strip()

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # datetime accepts at most 6 fractional digits; logs carry 9.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def fmt_ts(moment):
    if moment is None:
        return "n/a"

    text = moment.strftime("%Y-%m-%dT%H:%M:%S")

    if moment.microsecond:
        text += ".%03d" % (moment.microsecond // 1000)

    return text + "Z"


def seconds_between(later, earlier):
    if later is None or earlier is None:
        return None

    return (later - earlier).total_seconds()


def fmt_secs(value):
    if value is None:
        return "n/a"

    if abs(value - round(value)) > 0.05 and abs(value) < 10:
        return "%.1fs" % value

    return "%ds" % round(value)


# ---------------------------------------------------------------- Kubernetes data

def _conditions(obj):
    return {c.get("type"): c for c in (obj.get("status", {}).get("conditions") or [])}


def condition_time(obj, ctype, status="True"):
    cond = _conditions(obj).get(ctype)

    if cond and cond.get("status") == status:
        return parse_ts(cond.get("lastTransitionTime"))

    return None


def select_vllm_pod(pods, name=None):
    """
    The newest live pod of the vLLM Deployment: label app=vllm and owned by a
    ReplicaSet (this excludes a hand-made pod that happens to share the label).
    Returns (pod, warnings).
    """
    warnings = []
    candidates = []

    for pod in pods:
        meta = pod.get("metadata", {})

        if name and meta.get("name") != name:
            continue

        if meta.get("deletionTimestamp"):
            continue

        owners = meta.get("ownerReferences") or []

        if not name and not any(o.get("kind") == "ReplicaSet" for o in owners):
            continue

        candidates.append(pod)

    if not candidates:
        return None, warnings

    candidates.sort(key=lambda p: p["metadata"]["creationTimestamp"])
    chosen = candidates[-1]

    if len(candidates) > 1:
        warnings.append(
            "%d vLLM pods exist (%s); measuring the newest, %s"
            % (len(candidates), ", ".join(p["metadata"]["name"] for p in candidates), chosen["metadata"]["name"])
        )

    return chosen, warnings


def pod_info(pod):
    status = pod.get("status", {})
    container = next(
        (c for c in (status.get("containerStatuses") or []) if c.get("name") == CONTAINER),
        {}
    )
    state = container.get("state", {})
    unschedulable = _conditions(pod).get("PodScheduled")

    return {
        "name": pod["metadata"]["name"],
        "created": parse_ts(pod["metadata"].get("creationTimestamp")),
        "node": pod.get("spec", {}).get("nodeName"),
        "ip": status.get("podIP"),
        "phase": status.get("phase"),
        "unschedulable_at": (
            parse_ts(unschedulable.get("lastTransitionTime"))
            if unschedulable and unschedulable.get("status") == "False" else None
        ),
        "scheduled_at": condition_time(pod, "PodScheduled"),
        "ready_at": condition_time(pod, "Ready"),
        "started_at": parse_ts((state.get("running") or {}).get("startedAt")),
        "waiting_reason": (state.get("waiting") or {}).get("reason"),
        "restarts": container.get("restartCount", 0),
        "image": container.get("image") or next(
            (c.get("image") for c in pod.get("spec", {}).get("containers", []) if c.get("name") == CONTAINER),
            None
        ),
    }


_NOMINATED = re.compile(r"nodeclaim/([a-z0-9][a-z0-9.-]*)")
_PULLED_IN = re.compile(r"in ((?:\d+h)?(?:\d+m)?[\d.]+s)")


def _event_time(event):
    return parse_ts(event.get("firstTimestamp") or event.get("eventTime") or event.get("lastTimestamp"))


def parse_duration(text):
    total = 0.0

    for amount, unit in re.findall(r"([\d.]+)([hms])", text or ""):
        total += float(amount) * {"h": 3600, "m": 60, "s": 1}[unit]

    return total


def events_info(events):
    info = {"nominated": None, "nominated_at": None, "failed_scheduling": 0,
            "pulling_at": None, "pulled_at": None, "pull_seconds": None, "image_cached": None, "warnings": []}

    for event in sorted(events, key=lambda e: _event_time(e) or datetime.min.replace(tzinfo=timezone.utc)):
        reason = event.get("reason")
        when = _event_time(event)
        message = event.get("message", "")

        if reason == "Nominated":
            match = _NOMINATED.search(message)

            if match and info["nominated"] is None:
                info["nominated"] = match.group(1)
                info["nominated_at"] = when

        elif reason == "FailedScheduling":
            info["failed_scheduling"] += 1

        elif reason == "Pulling" and info["pulling_at"] is None:
            info["pulling_at"] = when

        elif reason == "Pulled" and info["pulled_at"] is None:
            info["pulled_at"] = when
            info["image_cached"] = "already present on machine" in message
            match = _PULLED_IN.search(message)

            if match:
                info["pull_seconds"] = parse_duration(match.group(1))

        elif reason in ("Failed", "BackOff") and event.get("type") == "Warning":
            info["warnings"].append("%s: %s" % (reason, message[:120]))

    return info


def find_nodeclaim(nodeclaims, node_name, nominated):
    """The NodeClaim whose node is the pod's node; before binding, the one Karpenter nominated."""
    for claim in nodeclaims:
        if node_name and claim.get("status", {}).get("nodeName") == node_name:
            return claim

    for claim in nodeclaims:
        if nominated and claim["metadata"]["name"] == nominated:
            return claim

    return None


def nodeclaim_info(claim):
    if claim is None:
        return None

    labels = claim["metadata"].get("labels", {})

    return {
        "name": claim["metadata"]["name"],
        "created": parse_ts(claim["metadata"].get("creationTimestamp")),
        "launched": condition_time(claim, "Launched"),
        "registered": condition_time(claim, "Registered"),
        "initialized": condition_time(claim, "Initialized"),
        "node": claim.get("status", {}).get("nodeName"),
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "zone": labels.get("topology.kubernetes.io/zone"),
    }


def node_info(node):
    if node is None:
        return None

    labels = node["metadata"].get("labels", {})

    return {
        "name": node["metadata"]["name"],
        "ready_at": condition_time(node, "Ready"),
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "gpu_allocatable": (node.get("status", {}).get("allocatable") or {}).get("nvidia.com/gpu"),
    }


def device_plugin_ready(pods):
    times = [condition_time(p, "Ready") for p in pods]
    times = [t for t in times if t]

    return min(times) if times else None


_TOOK = re.compile(r"\b(took|takes|finished in)\b.*?\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds)\b", re.IGNORECASE)
_LOG_PREFIX = re.compile(r"^(?:\(\w+ pid=\d+\)\s*)?(?:INFO|WARNING|DEBUG)\s+[\d-]+\s+[\d:]+(?:\s+\[[^\]]*\])?\s*")


def log_timings(log_text, started_at, limit=8):
    """
    What vLLM itself reported while starting: lines such as "Model loading took
    14.2 GiB and 31.5 seconds". Best effort: the wording depends on the vLLM
    version, and nothing is invented when no line matches.
    """
    found = []

    for line in log_text.splitlines():
        stamp, _, message = line.partition(" ")

        if not _TOOK.search(message):
            continue

        when = parse_ts(stamp)
        text = _LOG_PREFIX.sub("", message).strip()

        found.append((seconds_between(when, started_at), text[:150]))

        if len(found) >= limit:
            break

    return found


def log_app_started(log_text):
    """
    When vLLM said it could serve: the first "Application startup complete" log
    line (sub-second, from the container runtime's timestamps). Used as T4b
    when no live /health success was observed, for example when the script
    started after the pod was already Ready.
    """
    for line in log_text.splitlines():
        stamp, _, message = line.partition(" ")

        if "Application startup complete" in message:
            return parse_ts(stamp)

    return None


# ---------------------------------------------------------------- calculation

def effective_ready(pod_ready, health_ok):
    """
    Traffic can only reach the pod once it is Ready (the Service routes to Ready
    pods), and /health is the application's own answer. Both must hold, so the
    later of the two is the traffic-ready time.
    """
    known = [t for t in (pod_ready, health_ok) if t is not None]

    return max(known) if known else None


def milestones(pod, claim, node, health_ok, inference_end, plugin_ready=None, gpu_seen=None):
    t0 = pod["created"]
    t1 = claim["created"] if claim else None
    t2 = node["ready_at"] if node else None
    t2b = (claim["initialized"] if claim else None) or gpu_seen
    t3 = pod["started_at"]
    t4a = pod["ready_at"]
    t4 = effective_ready(t4a, health_ok)

    return {"T0": t0, "T1": t1, "T2": t2, "T2b": t2b, "T3": t3, "T4a": t4a,
            "T4b": health_ok, "T4": t4, "T5": inference_end, "plugin_ready": plugin_ready}


def durations(m, posthoc=False):
    """
    The headline numbers. After the fact (the pod was already Ready when the
    script started), T5 minus T4 only measures how late the script ran, so
    First request and Total usable cold start are not reported.
    """
    d = [
        ("Karpenter reaction", seconds_between(m["T1"], m["T0"])),
        ("Node provisioning", seconds_between(m["T2"], m["T1"])),
        ("Container scheduling", seconds_between(m["T3"], m["T2"])),
        ("Model initialization", seconds_between(m["T4"], m["T3"])),
        ("First request", seconds_between(m["T5"], m["T4"])),
    ]
    summary = [
        ("GPU capacity ready", seconds_between(m["T2"], m["T0"])),
        ("Model cold start", seconds_between(m["T4"], m["T3"])),
        ("Total usable cold start", seconds_between(m["T5"], m["T0"]) if m["T5"] else None),
    ]

    if m["T5"] is None:
        summary[2] = ("Pod ready (no inference)", seconds_between(m["T4"], m["T0"]))

    if posthoc:
        d[4] = ("First request", None)

        if m["T5"]:
            summary[2] = ("Total usable cold start", None)

    return d, summary


def breakdown(m, pod, claim, events, request_start, request_end):
    """Informational sub-steps that explain the headline numbers."""
    rows = [
        ("scheduler marked pod Unschedulable", seconds_between(pod["unschedulable_at"], m["T0"]), "after T0"),
        ("EC2 instance launched", seconds_between(claim["launched"] if claim else None, m["T1"]), "after T1"),
        ("node registered with the cluster", seconds_between(claim["registered"] if claim else None, claim["launched"] if claim else None), "after launch"),
        ("node Ready", seconds_between(m["T2"], claim["registered"] if claim else None), "after registration"),
        ("nvidia.com/gpu available (T2b)", seconds_between(m["T2b"], m["T2"]), "after T2"),
        ("pod bound to the GPU node", seconds_between(pod["scheduled_at"], m["T2"]), "after T2"),
        ("image pull started", seconds_between(events["pulling_at"], pod["scheduled_at"]), "after bind"),
        ("image pull", seconds_between(events["pulled_at"], events["pulling_at"]) if events["pulling_at"] else events["pull_seconds"], "pull duration"),
        ("container started", seconds_between(m["T3"], events["pulled_at"] or pod["scheduled_at"]), "after pull"),
        ("pod Ready after /health first passed", seconds_between(m["T4a"], m["T4b"]), "readiness probe lag"),
    ]

    if request_start and request_end:
        rows.append(("inference request latency", seconds_between(request_end, request_start), "one request"))

    return rows


def warnings_for(pod, claim, m, posthoc):
    out = []

    if claim and m["T1"] and m["T0"] and m["T1"] < m["T0"]:
        out.append("The NodeClaim is older than the pod: a GPU node already existed, so this is a warm start, not a cold start.")

    if pod["restarts"]:
        out.append("The vLLM container restarted %d time(s): T3 is its latest start, so Model initialization is not a clean cold start." % pod["restarts"])

    if posthoc:
        out.append("The pod was already Ready when the script started, so there was no live /health or inference observation: T4b comes from vLLM's "
                   "'Application startup complete' log line, and T5 (if any) is not a first-success time. The Kubernetes-recorded milestones are still exact.")

    for label, a, b in (("T0->T1", m["T0"], m["T1"]), ("T1->T2", m["T1"], m["T2"]), ("T2->T3", m["T2"], m["T3"]), ("T3->T4", m["T3"], m["T4"])):
        if a and b and b < a:
            out.append("%s is negative: timestamps are out of order (clock skew or 1 s resolution)." % label)

    return out


# ---------------------------------------------------------------- report

def format_report(m, pod, claim, node, events, logs, request_start, request_end, notes, extra):
    lines = ["vLLM Cold Start Measurement", ""]
    lines += ["Pod        %s" % pod["name"], "Image      %s" % (pod["image"] or "n/a")]

    if claim:
        lines.append("NodeClaim  %s (%s, %s)" % (claim["name"], claim["instance_type"] or "n/a", claim["zone"] or "n/a"))

    if node:
        lines.append("Node       %s (%s)" % (node["name"], node["instance_type"] or "n/a"))

    lines.append("")

    labelled = [
        ("T0 Pod Pending", m["T0"]), ("T1 NodeClaim Created", m["T1"]), ("T2 Node Ready", m["T2"]),
        ("T3 Container Started", m["T3"]), ("T4 vLLM Ready", m["T4"]), ("T5 First Inference", m["T5"]),
    ]

    for label, moment in labelled:
        lines += [label, fmt_ts(moment)]

        if label.startswith("T4") and m["T4a"] and m["T4b"]:
            lines[-1] += "   (pod Ready %s, /health %s; later one used)" % (fmt_ts(m["T4a"]), fmt_ts(m["T4b"]))

        lines.append("")

    steps, summary = durations(m, extra.get("posthoc"))

    lines.append("Durations")
    lines.append("")
    lines += ["%-26s %s" % (name, fmt_secs(value)) for name, value in steps]
    lines.append("")
    lines += ["%-26s %s" % (name, fmt_secs(value)) for name, value in summary]
    lines += ["", "Breakdown (informational)", ""]
    lines += ["%-36s %7s   %s" % (name, fmt_secs(value), note) for name, value, note in breakdown(m, pod, claim, events, request_start, request_end) if value is not None]

    if extra.get("nlb_healthy_at") and not extra.get("posthoc"):
        lines.append("%-36s %7s   %s" % ("NLB target healthy", fmt_secs(seconds_between(extra["nlb_healthy_at"], m["T4"])), "after T4 (inside First request)"))

    if m["plugin_ready"]:
        lines.append("%-36s %7s   %s" % ("device plugin pod Ready", fmt_secs(seconds_between(m["plugin_ready"], m["T2"])), "after T2"))

    if logs:
        lines += ["", "vLLM reported while starting (best effort, version dependent)", ""]
        lines += ["%7s   %s" % (fmt_secs(offset), text) for offset, text in logs]

    if notes:
        lines += ["", "Notes", ""] + ["- " + n for n in notes]

    lines += ["", "Kubernetes timestamps have 1 s resolution: each duration is +/- 1 s."]

    return "\n".join(lines)


def as_json(m, pod, claim, node, events, logs, request_start, request_end, notes, extra):
    steps, summary = durations(m, extra.get("posthoc"))

    return json.dumps({
        "pod": pod["name"], "image": pod["image"],
        "nodeclaim": claim["name"] if claim else None, "node": node["name"] if node else None,
        "instance_type": (claim or node or {}).get("instance_type") if (claim or node) else None,
        "timestamps": {k: fmt_ts(v) if v else None for k, v in m.items()},
        "durations_seconds": {name: value for name, value in steps + summary},
        "breakdown_seconds": {name: value for name, value, _ in breakdown(m, pod, claim, events, request_start, request_end)},
        "vllm_reported": [{"offset_seconds": o, "message": t} for o, t in logs],
        "notes": notes,
    }, indent=2)


# ---------------------------------------------------------------- kubectl / AWS / HTTP

class Kube:
    def __init__(self, namespace):
        self.namespace = namespace

    def run(self, args, timeout=30):
        try:
            result = subprocess.run(["kubectl"] + args, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise MeasurementError("kubectl is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise MeasurementError("kubectl %s timed out" % " ".join(args[:2]))

        if result.returncode != 0:
            raise MeasurementError("kubectl %s failed: %s" % (" ".join(args[:2]), result.stderr.strip()[:200]))

        return result.stdout

    def get(self, args, namespace=True):
        scope = ["-n", self.namespace] if namespace else []
        return json.loads(self.run(["get"] + args + scope + ["-o", "json"]))

    def pods(self):
        return self.get(["pods", "-l", POD_LABEL]).get("items", [])

    def events(self, pod_name):
        return self.get(["events", "--field-selector", "involvedObject.kind=Pod,involvedObject.name=" + pod_name]).get("items", [])

    def nodeclaims(self):
        return self.get(["nodeclaims"], namespace=False).get("items", [])

    def node(self, name):
        try:
            return self.get(["node", name], namespace=False)
        except MeasurementError:
            return None

    def plugin_pods(self, node_name):
        out = self.run(["get", "pods", "-n", DEVICE_PLUGIN_NAMESPACE, "-l", DEVICE_PLUGIN_LABEL,
                        "--field-selector", "spec.nodeName=" + node_name, "-o", "json"])
        return json.loads(out).get("items", [])

    def logs(self, pod_name):
        # The start of the log, not the end: startup is at the beginning and a
        # long-running pod's log fills up with /health probe lines.
        try:
            return self.run(["logs", "-n", self.namespace, pod_name, "-c", CONTAINER, "--timestamps", "--limit-bytes", "2000000"], timeout=60)
        except MeasurementError:
            return ""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PortForward:
    """
    kubectl port-forward to the vLLM pod, used only to ask /health directly.
    kubectl can exit when the container is not listening yet, so healthy()
    starts it again whenever it has died.
    """

    def __init__(self, namespace, pod_name):
        self.namespace = namespace
        self.pod_name = pod_name
        self.port = free_port()
        self.process = None
        self._start()

    def _start(self):
        self.process = subprocess.Popen(
            ["kubectl", "port-forward", "-n", self.namespace, "pod/" + self.pod_name, "%d:%d" % (self.port, VLLM_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def healthy(self):
        if self.process.poll() is not None:
            self._start()
            time.sleep(1)

        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()


def nlb_target_states():
    """Read-only: {target ip: state} for the vLLM load balancer's target groups (healthy, unhealthy, initial, draining, unused)."""

    def aws(args):
        result = subprocess.run(["aws"] + args + ["--output", "json"], capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise MeasurementError("aws %s failed: %s" % (args[1], result.stderr.strip()[:200]))

        return json.loads(result.stdout)

    groups = aws(["elbv2", "describe-target-groups"])["TargetGroups"]
    arns = [g["TargetGroupArn"] for g in groups if "k8s-default-vllm" in g["TargetGroupName"]]

    if not arns:
        raise MeasurementError("no k8s-default-vllm target group: is the vLLM Service (and its load balancer) deployed?")

    states = {}

    for arn in arns:
        for target in aws(["elbv2", "describe-target-health", "--target-group-arn", arn])["TargetHealthDescriptions"]:
            states[target["Target"]["Id"]] = target["TargetHealth"]["State"]

    return states


def nlb_target_healthy(pod_ip):
    """Is the pod's IP (or, without an IP, any target) healthy in the vLLM load balancer's target group?"""
    states = nlb_target_states()

    if pod_ip:
        return states.get(pod_ip) == "healthy"

    return "healthy" in states.values()


def gateway_settings():
    """URL and token from GATEWAY_URL / GATEWAY_TOKEN, else from the app stack's Terraform outputs and Cognito. Never printed."""
    url, token = os.environ.get("GATEWAY_URL"), os.environ.get("GATEWAY_TOKEN")

    if url and token:
        return url, token

    def output(name):
        result = subprocess.run(["terraform", "output", "-raw", name], cwd=APP_DIR, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            raise MeasurementError("cannot read terraform output '%s' (is the app stack applied?)" % name)

        return result.stdout.strip()

    url = output("invoke_url")
    basic = b64encode(("%s:%s" % (output("m2m_client_id"), output("m2m_client_secret"))).encode()).decode()
    request = urllib.request.Request(
        output("cognito_token_endpoint"),
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "enterprise-ai-gateway/invoke"}).encode(),
        headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            token = json.load(response)["access_token"]
    except Exception as exc:
        raise MeasurementError("could not get a Cognito token (%s)" % type(exc).__name__)

    return url, token


def classify_inference(status, body):
    """Success only for HTTP 200 with a model answer. The message never repeats the response text."""
    if status == 200 and isinstance(body, dict) and body.get("response"):
        return None

    code = body.get("error") if isinstance(body, dict) else None

    return "inference failed: HTTP %s%s" % (status, " (%s)" % code if code else "")


def call_inference(url, token, timeout=40):
    """
    One request through API Gateway. Never raises: returns what the caller saw.
    {"start", "end", "status" (None if the request itself failed), "error" (the gateway's error code),
     "exception" (client-side failure type), "problem" (None when the request succeeded)}.
    """
    payload = json.dumps({"provider": "vllm", "prompt": INFERENCE_PROMPT}).encode()
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "authorization": "Bearer " + token, "content-type": "application/json"})
    start = now_utc()
    status, body, exception = None, {}, None

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status = exc.code

        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
    except Exception as exc:
        exception = type(exc).__name__

    end = now_utc()

    return {
        "start": start, "end": end, "status": status, "exception": exception,
        "error": body.get("error") if isinstance(body, dict) else None,
        "problem": ("inference request failed (%s)" % exception) if exception else classify_inference(status, body),
    }


def send_inference(url, token):
    """One request. Returns (start, end). Raises MeasurementError on failure."""
    result = call_inference(url, token)

    if result["problem"]:
        raise MeasurementError(result["problem"])

    return result["start"], result["end"]


# ---------------------------------------------------------------- the measurement

def check_failures(pod, events, claim, waited_for_claim, args):
    """Fail early and clearly instead of waiting out the timeout."""
    if pod["waiting_reason"] in FATAL_WAITING_REASONS:
        raise MeasurementError("the vLLM container cannot start: %s%s" % (
            pod["waiting_reason"], (" (" + "; ".join(events["warnings"][-1:]) + ")") if events["warnings"] else ""))

    if claim is None and pod["node"] is None and waited_for_claim > args.nodeclaim_timeout:
        raise MeasurementError(
            "no Karpenter NodeClaim for the pod after %ds (%d FailedScheduling events). Check the NodePool limits and "
            "`kubectl -n kube-system logs deploy/karpenter`" % (waited_for_claim, events["failed_scheduling"]))


def measure(args, kube, out=print):
    started = time.monotonic()
    posthoc, port_forward, health_ok, gpu_seen = None, None, None, None
    notes = []

    def elapsed():
        return time.monotonic() - started

    pod = claim = node = events = None

    try:
        while True:
            pods = kube.pods()
            raw, pod_warnings = select_vllm_pod(pods, args.pod)

            if raw is None:
                if elapsed() > args.pod_timeout:
                    raise MeasurementError("no vLLM pod (label %s, owned by a ReplicaSet) appeared within %ds" % (POD_LABEL, args.pod_timeout))

                time.sleep(args.interval)
                continue

            pod = pod_info(raw)
            events = events_info(kube.events(pod["name"]))
            claim = nodeclaim_info(find_nodeclaim(kube.nodeclaims(), pod["node"], events["nominated"]))
            node = node_info(kube.node(pod["node"])) if pod["node"] else None
            check_failures(pod, events, claim, elapsed(), args)

            if posthoc is None:
                posthoc = pod["ready_at"] is not None
                notes += pod_warnings

            if node and node["gpu_allocatable"] and gpu_seen is None:
                gpu_seen = now_utc()

            if pod["started_at"] and port_forward is None and not posthoc:
                port_forward = PortForward(kube.namespace, pod["name"])

            if port_forward and health_ok is None and port_forward.healthy():
                health_ok = now_utc()

            if pod["ready_at"] and (posthoc or health_ok or elapsed() > args.timeout):
                break

            if elapsed() > args.timeout:
                raise MeasurementError("vLLM was not ready after %ds (phase: %s)" % (args.timeout, describe_phase(pod, claim, node)))

            time.sleep(args.interval)

        plugin = None

        if node:
            try:
                plugin = device_plugin_ready(kube.plugin_pods(node["name"]))
            except MeasurementError:
                plugin = None

        log_text = kube.logs(pod["name"])
        logs = log_timings(log_text, pod["started_at"])

        if health_ok is None:
            health_ok = log_app_started(log_text)

            if health_ok:
                notes.append("T4b is vLLM's 'Application startup complete' log time, not a live /health observation.")

        request_start = request_end = nlb_healthy_at = None

        if args.test_inference:
            url, token = gateway_settings()
            deadline = time.monotonic() + args.nlb_timeout

            while not nlb_target_healthy(pod["ip"]):
                if time.monotonic() > deadline:
                    raise MeasurementError("the pod never became a healthy load balancer target within %ds" % args.nlb_timeout)

                time.sleep(args.interval)

            nlb_healthy_at = now_utc()
            request_start, request_end = send_inference(url, token)

        m = milestones(pod, claim, node, health_ok, request_end, plugin, gpu_seen)
        notes += warnings_for(pod, claim, m, posthoc)

        if m["T2b"] and not (claim and claim["initialized"]):
            notes.append("T2b is when this script first saw nvidia.com/gpu allocatable (poll resolution), not a Kubernetes timestamp.")

        if posthoc and request_end:
            notes.append("Because the pod was already Ready, First request and Total usable cold start are not reported (they would only measure how late "
                         "the script ran). The inference request latency in the breakdown is the real single-request time.")

        extra = {"nlb_healthy_at": nlb_healthy_at, "posthoc": posthoc}
        formatter = as_json if args.json else format_report
        out(formatter(m, pod, claim, node, events, logs, request_start, request_end, notes, extra))

        return 0

    except MeasurementError as error:
        if error.partial is None:
            error.partial = partial_progress(pod, claim, node, health_ok)

        raise

    finally:
        if port_forward:
            port_forward.close()


def partial_progress(pod, claim, node, health_ok):
    """The milestones reached so far, for a failure message."""
    if pod is None:
        return ["no vLLM pod was seen"]

    rows = [("T0 pod Pending", pod["created"]), ("T1 NodeClaim created", claim["created"] if claim else None),
            ("T2 node Ready", node["ready_at"] if node else None), ("T3 container started", pod["started_at"]),
            ("T4a pod Ready", pod["ready_at"]), ("T4b /health passed", health_ok)]

    return ["%-22s %s" % (label, fmt_ts(moment)) for label, moment in rows]


def describe_phase(pod, claim, node):
    if pod["started_at"]:
        return "container running, waiting for readiness"

    if pod["node"]:
        return "pod bound to %s, container not started (image pull?)" % pod["node"]

    if node is None and claim:
        return "NodeClaim %s exists, node not Ready" % claim["name"]

    return "pod Pending"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure vLLM cold start on EKS from Kubernetes timestamps (read-only).")
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--pod", help="measure this pod instead of the newest vLLM pod")
    parser.add_argument("--test-inference", action="store_true", help="send one request through API Gateway once vLLM is ready")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the text report")
    parser.add_argument("--interval", type=float, default=3, help="seconds between polls (default 3)")
    parser.add_argument("--timeout", type=int, default=2400, help="give up if not Ready after this many seconds (default 2400)")
    parser.add_argument("--pod-timeout", type=int, default=300, help="give up if no vLLM pod appears (default 300)")
    parser.add_argument("--nodeclaim-timeout", type=int, default=180, help="give up if Karpenter makes no NodeClaim (default 180)")
    parser.add_argument("--nlb-timeout", type=int, default=300, help="give up waiting for a healthy load balancer target (default 300)")
    args = parser.parse_args(argv)

    try:
        return measure(args, Kube(args.namespace))
    except MeasurementError as error:
        print("FAILED: %s" % error, file=sys.stderr)

        for line in error.partial or []:
            print("  " + line, file=sys.stderr)

        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
