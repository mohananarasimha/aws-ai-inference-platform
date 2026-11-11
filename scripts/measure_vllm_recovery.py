#!/usr/bin/env python3
"""
Measure how the vLLM inference path recovers from three failures, from
Kubernetes' own timestamps plus a few controlled requests:

  pod         delete the vLLM pod (the GPU node survives)
  readiness   freeze the vLLM process in place: Running but not serving, then resume it
  node        terminate the GPU node's EC2 instance
  check       read-only: identify the targets, run the safety checks, print the exact commands

It MEASURES by default and injects nothing. A failure is only injected with
--inject, after the target is printed and you type its name (or pass the same
value with --confirm). Without --inject, pod and node runs wait for you to
cause the failure yourself and take the start time from Kubernetes or EC2.

  scripts/measure_vllm_recovery.py check
  scripts/measure_vllm_recovery.py pod --inject
  scripts/measure_vllm_recovery.py readiness --inject
  scripts/measure_vllm_recovery.py node --inject

Reuses scripts/measure_vllm_cold_start.py. Never prints tokens or credentials.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import timedelta

import measure_vllm_cold_start as cs

MeasurementError = cs.MeasurementError

EXPECTED_INSTANCE_TYPE = "g5.xlarge"
PROCESS_PATTERN = "[Q]wen2.5-7B-Instruct"   # the vLLM API server's command line; the [Q] keeps pgrep from matching itself
COLD_START_BASELINE = {"GPU capacity ready": 41, "Pod Ready": 701}   # docs/vllm-cold-start.md

MILESTONES = {
    "pod": [
        ("P0", "pod deletion requested (failure injected)"), ("P1", "replacement pod created"),
        ("P2", "replacement pod bound to a node"), ("P3", "vLLM container started"), ("P4a", "pod Ready"),
        ("P4b", "vLLM serving (its own log line)"), ("P4c", "NLB target healthy"), ("P5", "first successful inference")],
    "readiness": [
        ("R0", "vLLM process frozen (failure injected)"), ("R1", "pod Ready=False"),
        ("R2", "removed from Service endpoints"), ("R3", "NLB target first not healthy"),
        ("R4", "process resumed (recovery begins)"), ("R4b", "pod Ready=True again"),
        ("R5", "back in Service endpoints"), ("R6", "NLB target healthy"), ("R7", "first successful inference")],
    "node": [
        ("N0", "GPU node terminated (failure injected)"), ("N1", "Kubernetes sees the node unavailable"),
        ("N2", "replacement vLLM pod Pending"), ("N3", "Karpenter NodeClaim created"), ("N4", "new node Ready"),
        ("N5", "nvidia.com/gpu available"), ("N6", "vLLM container started"), ("N7", "vLLM Ready (pod Ready)"),
        ("N8", "NLB target healthy"), ("N9", "first successful inference")],
}
START = {"pod": "P0", "readiness": "R0", "node": "N0"}
END = {"pod": "P5", "readiness": "R7", "node": "N9"}

DURATIONS = {
    "pod": [
        ("replacement pod created", "P0", "P1"), ("scheduling delay", "P1", "P2"), ("container start", "P2", "P3"),
        ("container start to Ready (model startup)", "P3", "P4a"), ("NLB recovery after Ready", "P4a", "P4c"),
        ("pod Ready MTTR", "P0", "P4a"), ("TOTAL pod-failure MTTR", "P0", "P5")],
    "readiness": [
        ("kubelet readiness reaction (Ready=False)", "R0", "R1"), ("NLB detection (target not healthy)", "R0", "R3"),
        ("endpoint removal after Ready=False", "R1", "R2"), ("kubelet recovery after resume", "R4", "R4b"),
        ("endpoint restored after resume", "R4", "R5"), ("NLB healthy after resume", "R4", "R6"),
        ("recovery MTTR (resume to first inference)", "R4", "R7"), ("outage window (injected to first inference)", "R0", "R7")],
    "node": [
        ("node-failure detection", "N0", "N1"), ("replacement pod created", "N0", "N2"),
        ("Karpenter reaction (pod Pending to NodeClaim)", "N2", "N3"), ("new GPU node provisioning", "N3", "N4"),
        ("GPU advertised after node Ready", "N4", "N5"), ("container start after node Ready", "N4", "N6"),
        ("model initialization", "N6", "N7"), ("NLB recovery after Ready", "N7", "N8"),
        ("TOTAL node-failure MTTR", "N0", "N9")],
}


# ---------------------------------------------------------------- timeline

class Timeline:
    """Named moments, each with the source it came from. The first value set for a key wins."""

    def __init__(self):
        self.events = {}

    def set(self, key, when, source):
        if when is not None and key not in self.events:
            self.events[key] = (when, source)

    def get(self, key):
        return self.events.get(key, (None, None))[0]

    def source(self, key):
        return self.events.get(key, (None, None))[1]


def at_or_after(moment, reference):
    """Kubernetes timestamps are whole seconds, so compare against the reference truncated to the second."""
    return moment is not None and reference is not None and moment >= reference.replace(microsecond=0)


def durations(kind, tl):
    return [(label, cs.seconds_between(tl.get(b), tl.get(a))) for label, a, b in DURATIONS[kind]]


# ---------------------------------------------------------------- object state

def pod_state(raw):
    info = cs.pod_info(raw)
    meta = raw["metadata"]
    deletion = cs.parse_ts(meta.get("deletionTimestamp"))
    grace = meta.get("deletionGracePeriodSeconds")

    info["deleting"] = deletion is not None
    info["deletion_requested_at"] = deletion - timedelta(seconds=grace) if deletion and grace is not None else deletion
    info["not_ready_at"] = cs.condition_time(raw, "Ready", "False")

    return info


def node_state(raw):
    info = cs.node_info(raw)
    labels = raw["metadata"].get("labels", {})

    info["not_ready_at"] = cs.condition_time(raw, "Ready", "Unknown") or cs.condition_time(raw, "Ready", "False")
    info["provider_id"] = raw.get("spec", {}).get("providerID")
    info["nodepool"] = labels.get("karpenter.sh/nodepool")
    info["workload"] = labels.get("workload")

    return info


def instance_id_from(provider_id):
    match = re.search(r"/(i-[0-9a-f]+)$", provider_id or "")
    return match.group(1) if match else None


def replacement_pod(pods, old_name, not_before):
    """The newest live vLLM pod that is not the original one and was created after the failure."""
    found = [p for p in pods if p["name"] != old_name and not p["deleting"]
             and (not_before is None or p["created"] is None or at_or_after(p["created"], not_before))]

    return max(found, key=lambda p: p["created"]) if found else None


def model_cache(log_text):
    """
    Did the replacement pod download the model weights again? The cache is an emptyDir, so
    a new pod is expected to. Evidence is what vLLM itself reports; nothing is invented.
    """
    explicit = re.search(r"Time spent downloading weights[^:\n]*:\s*([\d.]+) seconds", log_text)
    weights = re.search(r"Loading weights took ([\d.]+) seconds", log_text)
    total = re.search(r"Model loading took [\d.]+ GiB memory and ([\d.]+) seconds", log_text)

    if explicit:
        seconds, evidence = float(explicit.group(1)), "vLLM reported the download time"
    elif weights and total:
        seconds = max(float(total.group(1)) - float(weights.group(1)), 0.0)
        evidence = "model loading minus weight loading (estimate)"
    else:
        return {"reused": None, "download_seconds": None, "evidence": "vLLM did not report download or loading times"}

    return {"reused": seconds < 5, "download_seconds": seconds, "evidence": evidence}


# ---------------------------------------------------------------- Kubernetes and AWS snapshots

def run_command(argv, timeout=60):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise MeasurementError("%s is not installed or not on PATH" % argv[0])
    except subprocess.TimeoutExpired:
        raise MeasurementError("%s timed out" % " ".join(argv[:3]))

    if result.returncode != 0:
        raise MeasurementError("%s failed: %s" % (" ".join(argv[:3]), result.stderr.strip()[:200]))

    return result.stdout


def endpoint_ready_ips(kube):
    ips = set()

    for piece in kube.get(["endpointslices", "-l", "kubernetes.io/service-name=vllm"]).get("items", []):
        for endpoint in piece.get("endpoints") or []:
            if endpoint.get("conditions", {}).get("ready") is not False:
                ips.update(endpoint.get("addresses") or [])

    return ips


def take_snapshot(kube, nlb_states):
    snapshot = {
        "now": cs.now_utc(),
        "pods": [pod_state(p) for p in kube.get(["pods", "-l", cs.POD_LABEL]).get("items", [])],
        "nodes": {},
        "claims": [cs.nodeclaim_info(c) for c in kube.nodeclaims()],
        "eps_ready": endpoint_ready_ips(kube),
        "nlb": None,
    }

    for raw in kube.get(["nodes", "-l", "workload=gpu"], namespace=False).get("items", []):
        state = node_state(raw)
        snapshot["nodes"][state["name"]] = state

    if nlb_states is not None:
        try:
            snapshot["nlb"] = nlb_states()
        except MeasurementError:
            snapshot["nlb"] = None

    return snapshot


def ec2_instance(instance_id):
    """Read-only: state, type, tags and (once it is going away) the time EC2 says it was terminated."""
    out = run_command(["aws", "ec2", "describe-instances", "--instance-ids", instance_id, "--output", "json"], 30)
    instances = [i for r in json.loads(out)["Reservations"] for i in r["Instances"]]

    if not instances:
        raise MeasurementError("EC2 does not know instance %s" % instance_id)

    raw = instances[0]
    reason = raw.get("StateTransitionReason", "")
    match = re.search(r"\((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) GMT\)", reason)

    return {
        "id": instance_id, "state": raw["State"]["Name"], "type": raw["InstanceType"],
        "tags": {t["Key"]: t["Value"] for t in raw.get("Tags", [])},
        "terminated_at": cs.parse_ts(match.group(1).replace(" ", "T") + "Z") if match else None,
    }


def ssm_online(instance_id):
    try:
        out = run_command(["aws", "ssm", "describe-instance-information", "--filters",
                           "Key=InstanceIds,Values=" + instance_id, "--query",
                           "InstanceInformationList[].PingStatus", "--output", "text"], 30)
    except MeasurementError:
        return False

    return out.strip() == "Online"


# ---------------------------------------------------------------- identifying the target and safety

def identify(kube, pod_name=None, ec2=ec2_instance, ssm=ssm_online):
    raw, warnings = cs.select_vllm_pod(kube.pods(), pod_name)

    if raw is None:
        raise MeasurementError("no vLLM pod found (label %s, owned by a ReplicaSet)" % cs.POD_LABEL)

    pod = pod_state(raw)
    node_raw = kube.node(pod["node"]) if pod["node"] else None
    claims = kube.nodeclaims()
    claim = cs.nodeclaim_info(cs.find_nodeclaim(claims, pod["node"], None))
    node = node_state(node_raw) if node_raw else None
    instance_id = instance_id_from(node["provider_id"]) if node else None

    return {
        "pod": pod, "node": node, "claim": claim, "instance_id": instance_id,
        "namespace": kube.namespace, "warnings": warnings,
        "gpu_nodes": [n["metadata"]["name"] for n in kube.get(["nodes", "-l", "workload=gpu"], namespace=False).get("items", [])],
        "node_pods": [p for p in kube.get(["pods", "-A", "--field-selector", "spec.nodeName=" + pod["node"]], namespace=False).get("items", [])] if pod["node"] else [],
        "ec2": ec2(instance_id) if instance_id else None,
        "ssm_online": ssm(instance_id) if instance_id else False,
    }


def non_daemonset_pods(raw_pods):
    return [p["metadata"]["name"] for p in raw_pods
            if not any(o.get("kind") == "DaemonSet" for o in p["metadata"].get("ownerReferences") or [])]


def safety_problems(kind, ctx, instance_type=EXPECTED_INSTANCE_TYPE):
    """Reasons NOT to inject. An empty list means the target is the one we expect."""
    problems = []
    pod, node, claim, ec2 = ctx["pod"], ctx["node"], ctx["claim"], ctx["ec2"]

    if pod["ready_at"] is None:
        problems.append("the vLLM pod %s is not Ready: there is no healthy workload to fail" % pod["name"])

    if pod["deleting"]:
        problems.append("the vLLM pod is already being deleted")

    if kind == "pod":
        return problems

    if node is None:
        problems.append("the pod's node %s cannot be read" % pod["node"])
        return problems

    if node["workload"] != "gpu" or node["nodepool"] != "gpu":
        problems.append("node %s is not a Karpenter GPU node (workload=%s, nodepool=%s)" % (node["name"], node["workload"], node["nodepool"]))

    if node["instance_type"] != instance_type:
        problems.append("node %s is %s, expected %s" % (node["name"], node["instance_type"], instance_type))

    if claim is None:
        problems.append("no Karpenter NodeClaim maps to node %s" % node["name"])

    if len(ctx["gpu_nodes"]) != 1:
        problems.append("expected exactly one GPU node, found %d (%s)" % (len(ctx["gpu_nodes"]), ", ".join(ctx["gpu_nodes"])))

    others = [n for n in non_daemonset_pods(ctx["node_pods"]) if n != pod["name"]]

    if others:
        problems.append("other workloads run on this node: %s" % ", ".join(others))

    if ctx["instance_id"] is None:
        problems.append("cannot map node %s to an EC2 instance" % node["name"])

    if ec2 is None or ec2["state"] != "running":
        problems.append("the EC2 instance is not running (%s)" % (ec2["state"] if ec2 else "unknown"))
    else:
        if ec2["type"] != instance_type:
            problems.append("EC2 says the instance is %s, expected %s" % (ec2["type"], instance_type))

        if ec2["tags"].get("karpenter.sh/nodepool") != "gpu":
            problems.append("the EC2 instance is not tagged karpenter.sh/nodepool=gpu")

        if claim and ec2["tags"].get("karpenter.sh/nodeclaim") != claim["name"]:
            problems.append("the EC2 instance's NodeClaim tag does not match %s" % claim["name"])

    if kind == "readiness" and not ctx["ssm_online"]:
        problems.append("the node is not Online in SSM, so the process cannot be frozen and resumed")

    return problems


# ---------------------------------------------------------------- injection commands

def pod_inject_command(ctx):
    return ["kubectl", "delete", "pod", ctx["pod"]["name"], "-n", ctx["namespace"], "--wait=false"]


def node_inject_command(ctx):
    return ["aws", "ec2", "terminate-instances", "--instance-ids", ctx["instance_id"]]


def freeze_script(pattern=PROCESS_PATTERN):
    if not re.fullmatch(r"[A-Za-z0-9\[\]./_-]+", pattern):
        raise MeasurementError("unsafe process pattern")

    return ("pid=$(pgrep -f '%s' | head -1); if [ -z \"$pid\" ]; then echo NOPID; exit 1; fi; "
            "kill -STOP \"$pid\" && echo \"STOPPED $pid\"" % pattern)


def verify_script(pattern=PROCESS_PATTERN):
    """Read-only: list the processes the freeze would match. Sends no signal."""
    if not re.fullmatch(r"[A-Za-z0-9\[\]./_-]+", pattern):
        raise MeasurementError("unsafe process pattern")

    return "pgrep -af '%s' | cut -c1-160" % pattern


def resume_script(pid):
    if not re.fullmatch(r"\d+", str(pid)):
        raise MeasurementError("unsafe pid")

    return "kill -CONT %s && echo \"CONTINUED %s\"" % (pid, pid)


def ssm_command(instance_id, script):
    return ["aws", "ssm", "send-command", "--instance-ids", instance_id, "--document-name", "AWS-RunShellScript",
            "--parameters", json.dumps({"commands": [script]}), "--query", "Command.CommandId", "--output", "text"]


def run_on_node(instance_id, script, runner=run_command, sleep=time.sleep):
    """Run a shell snippet on the node through SSM. Returns (output, finished_at)."""
    command_id = runner(ssm_command(instance_id, script), 60).strip()

    for _ in range(30):
        sleep(2)
        out = runner(["aws", "ssm", "get-command-invocation", "--command-id", command_id, "--instance-id", instance_id,
                      "--query", "[Status,StandardOutputContent,ExecutionEndDateTime]", "--output", "json"], 30)
        status, output, finished = json.loads(out)

        if status in ("Success", "Failed", "TimedOut", "Cancelled"):
            if status != "Success":
                raise MeasurementError("the command on the node ended as %s: %s" % (status, (output or "").strip()[:120]))

            return output, cs.parse_ts(finished)

    raise MeasurementError("the command on the node did not finish in time")


def confirmation_ok(expected, provided, ask=input):
    """The target's name must be typed (or passed with --confirm). Anything else cancels."""
    answer = provided if provided is not None else ask("Type %s to inject the failure: " % expected)

    return answer.strip() == expected


# ---------------------------------------------------------------- watching the recovery

def update_pod(tl, ctx, snap):
    old = next((p for p in snap["pods"] if p["name"] == ctx["pod"]["name"]), None)
    notes = []

    if old and old["deleting"]:
        tl.set("P0", old["deletion_requested_at"], "k8s (deletionTimestamp minus grace)")

    new = replacement_pod(snap["pods"], ctx["pod"]["name"], tl.get("P0") or ctx["pod"]["created"])

    if new:
        ctx["new_pod"] = new
        tl.set("P1", new["created"], "k8s")
        tl.set("P2", new["scheduled_at"], "k8s")
        tl.set("P3", new["started_at"], "k8s")
        tl.set("P4a", new["ready_at"], "k8s")

        if new["ip"] and snap["nlb"] is not None and new["ready_at"] and snap["nlb"].get(new["ip"]) == "healthy":
            tl.set("P4c", snap["now"], "observed (poll)")

    stray = [c for c in snap["claims"] if c["name"] != ctx["claim"]["name"] and at_or_after(c["created"], tl.get("P0") or ctx["pod"]["created"])] if ctx["claim"] else []

    if stray:
        notes.append("Karpenter created NodeClaim %s while the pod was being replaced (the NodePool limits should prevent a second GPU node)." % stray[0]["name"])

    return notes


def update_readiness(tl, ctx, snap):
    pod = next((p for p in snap["pods"] if p["name"] == ctx["pod"]["name"]), None)
    f0, r4, ip = tl.get("R0"), tl.get("R4"), ctx["pod"]["ip"]

    if pod is None or f0 is None:
        return []

    if r4 is None:
        if at_or_after(pod["not_ready_at"], f0):
            tl.set("R1", pod["not_ready_at"], "k8s")

        if ip not in snap["eps_ready"]:
            tl.set("R2", snap["now"], "observed (poll)")

        if snap["nlb"] is not None and snap["nlb"].get(ip, "absent") not in ("healthy", "initial"):
            tl.set("R3", snap["now"], "observed (poll, state=%s)" % snap["nlb"].get(ip, "absent"))

        return []

    # After the resume: anything that never failed counts as restored at the resume.
    if tl.get("R1") is None:
        tl.set("R4b", r4, "never lost")
    elif at_or_after(pod["ready_at"], r4):
        tl.set("R4b", pod["ready_at"], "k8s")

    if tl.get("R2") is None:
        tl.set("R5", r4, "never lost")
    elif ip in snap["eps_ready"]:
        tl.set("R5", snap["now"], "observed (poll)")

    if tl.get("R3") is None:
        tl.set("R6", r4, "never lost")
    elif snap["nlb"] is not None and snap["nlb"].get(ip) == "healthy":
        tl.set("R6", snap["now"], "observed (poll)")

    return []


def update_node(tl, ctx, snap):
    f0, old_node, old_claim = tl.get("N0"), (ctx["node"] or {}).get("name"), (ctx["claim"] or {}).get("name")

    if f0 is None:
        return []

    node = snap["nodes"].get(old_node)

    if node is None:
        tl.set("N1", snap["now"], "observed (node object removed)")
    elif at_or_after(node["not_ready_at"], f0):
        tl.set("N1", node["not_ready_at"], "k8s (Ready condition)")

    new = replacement_pod(snap["pods"], ctx["pod"]["name"], f0)
    claims = [c for c in snap["claims"] if c["name"] != old_claim and at_or_after(c["created"], f0)]
    claim = max(claims, key=lambda c: c["created"]) if claims else None

    if claim:
        tl.set("N3", claim["created"], "k8s")
        tl.set("N5", claim["initialized"], "k8s (NodeClaim Initialized)")
        fresh = snap["nodes"].get(claim["node"]) if claim["node"] and claim["node"] != old_node else None

        if fresh:
            tl.set("N4", fresh["ready_at"], "k8s")

    if new:
        ctx["new_pod"] = new
        tl.set("N2", new["created"], "k8s")
        tl.set("N6", new["started_at"], "k8s")
        tl.set("N7", new["ready_at"], "k8s")

        if new["ip"] and snap["nlb"] is not None and new["ready_at"] and snap["nlb"].get(new["ip"]) == "healthy":
            tl.set("N8", snap["now"], "observed (poll)")

    return []


UPDATERS = {"pod": update_pod, "readiness": update_readiness, "node": update_node}
READY_KEYS = {"pod": ("P4c",), "readiness": ("R4b", "R5", "R6"), "node": ("N8",)}   # infrastructure ready: only the inference request is left


def ready_time(kind, tl):
    moments = [tl.get(k) for k in READY_KEYS[kind]]

    return max(moments) if all(moments) else None


# ---------------------------------------------------------------- probes (the user's view)

def describe_result(result):
    if result["problem"] is None:
        return "success"

    if result["status"] is None:
        return "no HTTP response (%s)" % result["exception"]

    meaning = {502: "upstream error", 503: "endpoint not found", 504: "timeout"}.get(result["status"], "error")

    return "HTTP %d %s%s" % (result["status"], meaning, " (%s)" % result["error"] if result["error"] else "")


class Prober:
    """A few controlled requests while recovery is in progress. Never more than `maximum`, never two at once."""

    def __init__(self, call, first_delay, interval, maximum, clock):
        self.call, self.first_delay, self.interval, self.maximum, self.clock = call, first_delay, interval, maximum, clock
        self.results, self.started_at, self.thread, self.last = [], None, None, None

    def tick(self, now, window_start):
        if window_start is None or len(self.results) >= self.maximum or (self.thread and self.thread.is_alive()):
            return

        due = (self.last is None and now >= window_start + timedelta(seconds=self.first_delay)) or \
              (self.last is not None and now >= self.last + timedelta(seconds=self.interval))

        if not due:
            return

        self.last = now
        self.thread = threading.Thread(target=self._one, daemon=True)
        self.thread.start()

    def _one(self):
        result = self.call()
        self.results.append(result)

    def finish(self):
        if self.thread:
            self.thread.join(timeout=60)

    def first_success_after(self, moment):
        wins = [r["end"] for r in self.results if r["problem"] is None and (moment is None or r["end"] >= moment)]

        return min(wins) if wins else None


# ---------------------------------------------------------------- the run

class Run:
    def __init__(self, kind, args, kube, nlb_states=cs.nlb_target_states, gateway=cs.gateway_settings,
                 call=cs.call_inference, runner=run_command, sleep=time.sleep, ask=input, out=print,
                 ec2=ec2_instance, ssm=ssm_online):
        self.kind, self.args, self.kube = kind, args, kube
        self.ec2, self.ssm = ec2, ssm
        self.nlb_states, self.gateway, self.call, self.runner = nlb_states, gateway, call, runner
        self.sleep, self.ask, self.out = sleep, ask, out
        self.tl, self.notes, self.ctx, self.prober, self.frozen_pid = Timeline(), [], None, None, None
        self.final_results = []
        self.started = time.monotonic()

    def elapsed(self):
        return time.monotonic() - self.started

    # -- target and injection ----------------------------------------------------------------
    def target_lines(self):
        c = self.ctx
        lines = ["Target (before the failure)", "  pod         %s (%s, IP %s)" % (c["pod"]["name"], c["pod"]["phase"], c["pod"]["ip"])]

        if c["node"]:
            lines.append("  node        %s (%s)" % (c["node"]["name"], c["node"]["instance_type"]))

        if c["claim"]:
            lines.append("  NodeClaim   %s (%s)" % (c["claim"]["name"], c["claim"]["zone"]))

        if c["instance_id"]:
            lines.append("  instance    %s (%s)" % (c["instance_id"], c["ec2"]["state"] if c["ec2"] else "unknown"))

        return lines

    def planned_command(self):
        if self.kind == "pod":
            return " ".join(pod_inject_command(self.ctx))

        if self.ctx["instance_id"] is None:
            return "(the node cannot be mapped to an EC2 instance)"

        if self.kind == "node":
            return " ".join(node_inject_command(self.ctx))

        return "freeze (SIGSTOP) the vLLM API server on %s through SSM: %s" % (self.ctx["instance_id"], freeze_script())

    def expected_token(self):
        return self.ctx["instance_id"] if self.kind == "node" else self.ctx["pod"]["name"]

    def inject(self):
        problems = safety_problems(self.kind, self.ctx, self.args.instance_type)

        if problems:
            raise MeasurementError("not injecting, safety checks failed:\n  - " + "\n  - ".join(problems))

        self.out("\n".join(self.target_lines() + ["", "About to run:", "  " + self.planned_command(), ""]))

        if not confirmation_ok(self.expected_token(), self.args.confirm, self.ask):
            raise MeasurementError("cancelled: the confirmation did not match; nothing was injected")

        before = cs.now_utc()

        if self.kind == "pod":
            self.runner(pod_inject_command(self.ctx), 60)
            self.tl.set("P0", before, "script clock at the delete command")
        elif self.kind == "node":
            self.runner(node_inject_command(self.ctx), 60)
            self.tl.set("N0", before, "script clock at the terminate command")
        else:
            output, finished = run_on_node(self.ctx["instance_id"], freeze_script(), self.runner, self.sleep)
            match = re.search(r"STOPPED (\d+)", output or "")

            if not match:
                raise MeasurementError("the process was not frozen: %s" % (output or "").strip()[:120])

            self.frozen_pid = match.group(1)
            self.tl.set("R0", finished or cs.now_utc(), "SSM ExecutionEndDateTime")
            self.out("Frozen vLLM process %s. If this script dies, resume it with:\n  %s" % (
                self.frozen_pid, " ".join(ssm_command(self.ctx["instance_id"], resume_script(self.frozen_pid)))))

    def resume(self):
        output, finished = run_on_node(self.ctx["instance_id"], resume_script(self.frozen_pid), self.runner, self.sleep)

        if "CONTINUED" not in (output or ""):
            raise MeasurementError("the process was not resumed: %s" % (output or "").strip()[:120])

        self.tl.set("R4", finished or cs.now_utc(), "SSM ExecutionEndDateTime")
        self.frozen_pid = None

    def wait_for_failure(self):
        """Without --inject: wait for the failure the user causes, and take its start from Kubernetes or EC2."""
        deadline = time.monotonic() + self.args.inject_timeout

        while time.monotonic() < deadline:
            if self.kind == "pod":
                pod = next((p for p in (pod_state(r) for r in self.kube.get(["pods", "-l", cs.POD_LABEL]).get("items", []))
                            if p["name"] == self.ctx["pod"]["name"]), None)

                if pod is None or pod["deleting"]:
                    self.tl.set("P0", pod["deletion_requested_at"] if pod else cs.now_utc(), "k8s (deletionTimestamp minus grace)")
                    return
            else:
                info = self.ctx["ec2"] and self.ec2(self.ctx["instance_id"])

                if info and info["state"] != "running":
                    self.tl.set("N0", info["terminated_at"] or cs.now_utc(), "EC2 StateTransitionReason" if info["terminated_at"] else "first seen (poll)")
                    return

            self.sleep(self.args.interval)

        raise MeasurementError("no failure was injected within %ds" % self.args.inject_timeout)

    # -- observing ------------------------------------------------------------------------------
    def observe(self):
        last_aws = None

        while True:
            poll_aws = last_aws is None or (cs.now_utc() - last_aws).total_seconds() >= self.args.aws_interval
            snap = take_snapshot(self.kube, self.nlb_states if poll_aws else None)

            if poll_aws:
                last_aws = snap["now"]

            if snap["nlb"] is None:
                snap["nlb"] = self.last_nlb
            else:
                self.last_nlb = snap["nlb"]

            self.notes += [n for n in UPDATERS[self.kind](self.tl, self.ctx, snap) if n not in self.notes]

            if self.prober:
                self.prober.tick(snap["now"], self.tl.get(START[self.kind]))

            if self.kind == "readiness":
                self.readiness_driver(snap)

            if ready_time(self.kind, self.tl):
                if self.prober:
                    self.prober.finish()

                return

            if self.elapsed() > self.args.timeout:
                raise MeasurementError("no recovery within %ds (stuck after %s)" % (self.args.timeout, self.last_milestone()))

            self.sleep(self.args.interval)

    last_nlb = None

    def readiness_driver(self, snap):
        """Hold the freeze until the failure is seen everywhere (or --max-freeze), then resume."""
        if self.frozen_pid is None:
            return

        seen_all = all(self.tl.get(k) for k in ("R1", "R2", "R3"))
        held = (snap["now"] - self.tl.get("R0")).total_seconds()

        if seen_all or held >= self.args.max_freeze:
            if not seen_all:
                self.notes.append("Resumed after --max-freeze (%ds) without seeing %s." % (
                    self.args.max_freeze, ", ".join(k for k in ("R1", "R2", "R3") if not self.tl.get(k))))

            self.resume()

    def last_milestone(self):
        done = [k for k, _ in MILESTONES[self.kind] if self.tl.get(k)]

        return done[-1] if done else "the start"

    # -- evidence from the replacement pod -------------------------------------------------------
    def collect_evidence(self):
        new = self.ctx.get("new_pod")
        evidence = {"same_node": None, "image_cached": None, "model": None, "restarts": None}

        if self.kind == "readiness" or not new:
            return evidence

        events = cs.events_info(self.kube.events(new["name"]))
        log_text = self.kube.logs(new["name"])
        evidence.update({"same_node": new["node"] == self.ctx["node"]["name"] if self.ctx["node"] else None,
                         "image_cached": events["image_cached"], "pull_seconds": events["pull_seconds"],
                         "model": model_cache(log_text), "restarts": new["restarts"]})

        if self.kind == "pod":
            self.tl.set("P4b", cs.log_app_started(log_text), "vLLM log (Application startup complete)")

        return evidence

    # -- the whole run -----------------------------------------------------------------------------
    def execute(self):
        if self.kind == "readiness" and not self.args.inject:
            raise MeasurementError("readiness needs --inject: the script has to freeze and later resume the process itself")

        try:
            return self._execute()
        finally:
            if self.frozen_pid:
                # Never leave a process frozen: resume it even if the run failed or was interrupted.
                pid = self.frozen_pid

                try:
                    self.resume()
                    self.out("The frozen vLLM process %s was resumed." % pid)
                except MeasurementError as error:
                    self.out("COULD NOT RESUME the frozen vLLM process (%s). Resume it with:\n  %s" % (
                        error, " ".join(ssm_command(self.ctx["instance_id"], resume_script(pid)))))

    def _execute(self):
        self.ctx = identify(self.kube, self.args.pod, self.ec2, self.ssm)
        self.notes += self.ctx["warnings"]

        if self.args.inject:
            self.inject()
        else:
            problems = safety_problems(self.kind, self.ctx, self.args.instance_type)
            self.out("\n".join(self.target_lines()))

            if problems:
                raise MeasurementError("this is not the expected target:\n  - " + "\n  - ".join(problems))

            self.out("\nWaiting for you to cause the failure, for example:\n  %s\n" % self.planned_command())
            self.wait_for_failure()

        url = token = None

        if not self.args.no_inference:
            url, token = self.gateway()
            self.prober = Prober(lambda: self.call(url, token), self.args.first_probe_delay,
                                 self.args.probe_interval, self.args.probes, cs.now_utc)

        self.observe()
        self.evidence = self.collect_evidence()
        anchor = ready_time(self.kind, self.tl)

        if self.args.no_inference:
            self.tl.set(END[self.kind], anchor, "infrastructure ready (no request sent)")
        else:
            # One verification request once the infrastructure is ready. A target can be healthy a moment before
            # the first request is routed to it, so a failed attempt is retried a couple of times and every attempt is reported.
            final = None

            for attempt in range(self.args.final_attempts):
                final = self.call(url, token)
                self.final_results.append(final)

                if final["problem"] is None:
                    break

                if attempt < self.args.final_attempts - 1:
                    self.sleep(self.args.final_retry_delay)

            if final["problem"]:
                raise MeasurementError("final inference failed after recovery (%d attempt(s)): %s" % (len(self.final_results), final["problem"]))

            if len(self.final_results) > 1:
                self.notes.append("The first verification request after the infrastructure was ready failed (%s): the target was healthy before traffic was served." % describe_result(self.final_results[0]))

            self.tl.set(END[self.kind], self.prober.first_success_after(anchor) or final["end"], "first successful request after the infrastructure was ready")
            early = [r for r in self.prober.results if r["problem"] is None and r["end"] < anchor]

            if early:
                self.notes.append("%d request(s) succeeded before the replacement was ready (+%s): served by the old pod or a draining target, so not counted as recovery." % (
                    len(early), ", +".join(cs.fmt_secs(cs.seconds_between(r["start"], self.tl.get(START[self.kind]))) for r in early)))

        return self.report(self.evidence)

    evidence = None

    def report(self, evidence):
        return format_report(self.kind, self.tl, self.ctx, evidence, self.all_results(), self.notes, self.tl.get(START[self.kind]))

    def all_results(self):
        return (self.prober.results if self.prober else []) + self.final_results


# ---------------------------------------------------------------- report

def format_report(kind, tl, ctx, evidence, probes, notes, start):
    titles = {"pod": "pod failure", "readiness": "readiness failure (process frozen)", "node": "GPU node failure"}
    lines = ["vLLM Failure Recovery: " + titles[kind], ""]
    lines += ["Target (before the failure)", "  pod         %s" % ctx["pod"]["name"]]

    if ctx["node"]:
        lines.append("  node        %s (%s)" % (ctx["node"]["name"], ctx["node"]["instance_type"]))

    if ctx["claim"]:
        lines.append("  NodeClaim   %s" % ctx["claim"]["name"])

    lines += ["", "Timeline"]

    for key, label in MILESTONES[kind]:
        moment = tl.get(key)
        lines.append("  %-4s %-46s %s%s" % (key, label, cs.fmt_ts(moment), ("   [%s]" % tl.source(key)) if moment else ""))

    lines += ["", "Durations"]
    lines += ["  %-48s %s" % (label, cs.fmt_secs(value)) for label, value in durations(kind, tl) if value is not None]

    if kind != "readiness":
        lines += ["", "Recovery evidence"]

        if evidence["image_cached"] is not None:
            lines.append("  image                 %s" % ("already cached on the node" if evidence["image_cached"]
                                                         else "pulled again (%s)" % cs.fmt_secs(evidence.get("pull_seconds"))))

        if evidence["same_node"] is not None:
            lines.append("  replacement on node   %s" % ("the same node" if evidence["same_node"] else "a different node"))

        if evidence["model"]:
            m = evidence["model"]
            lines.append("  model weights         %s (%s%s)" % (
                {True: "cache reused", False: "downloaded again", None: "unknown"}[m["reused"]], m["evidence"],
                ", about %s" % cs.fmt_secs(m["download_seconds"]) if m["download_seconds"] is not None else ""))

        if evidence["restarts"]:
            lines.append("  container restarts    %d" % evidence["restarts"])

    if kind == "node" and tl.get("N0") and tl.get("N7"):
        lines += ["", "Against the cold-start baseline",
                  "  GPU capacity ready    %s now vs %ss then" % (cs.fmt_secs(cs.seconds_between(tl.get("N4"), tl.get("N2"))), COLD_START_BASELINE["GPU capacity ready"]),
                  "  pod Ready             %s after the replacement pod appeared vs %ss then" % (cs.fmt_secs(cs.seconds_between(tl.get("N7"), tl.get("N2"))), COLD_START_BASELINE["Pod Ready"])]

    if probes:
        lines += ["", "What the caller saw (controlled requests)"]

        for result in sorted(probes, key=lambda r: r["start"]):
            offset = cs.seconds_between(result["start"], start) if start else None
            lines.append("  %7s  %-48s %s" % ("+" + cs.fmt_secs(offset) if offset is not None else "?", describe_result(result),
                                               cs.fmt_secs(cs.seconds_between(result["end"], result["start"]))))

    if notes:
        lines += ["", "Notes"] + ["  - " + n for n in notes]

    lines += ["", "Kubernetes timestamps have 1 s resolution; 'observed (poll)' moments are as precise as the poll interval."]

    return "\n".join(lines)


def as_json(kind, tl, ctx, evidence, probes, notes, start):
    return json.dumps({
        "experiment": kind, "pod": ctx["pod"]["name"], "node": ctx["node"]["name"] if ctx["node"] else None,
        "timeline": {k: {"at": cs.fmt_ts(v[0]), "source": v[1]} for k, v in tl.events.items()},
        "durations_seconds": dict(durations(kind, tl)),
        "evidence": {k: v for k, v in evidence.items()},
        "probes": [{"offset_seconds": cs.seconds_between(r["start"], start) if start else None,
                    "result": describe_result(r), "latency_seconds": cs.seconds_between(r["end"], r["start"])} for r in probes],
        "notes": notes,
    }, indent=2, default=str)


def check(kube, args, out=print, runner=run_command, sleep=time.sleep):
    """Read-only: identify the targets and say whether each experiment is safe to run."""
    ctx = identify(kube, args.pod)
    run = Run("node", args, kube)
    run.ctx = ctx
    out("\n".join(run.target_lines()))
    out("  SSM         %s" % ("Online" if ctx["ssm_online"] else "not Online"))

    for kind in ("pod", "readiness", "node"):
        problems = safety_problems(kind, ctx, args.instance_type)
        run.kind = kind
        out("\n%s: %s" % (kind, "OK to run" if not problems else "BLOCKED"))
        out("  would run: " + run.planned_command())
        out("  type to confirm: " + run.expected_token())
        out("".join("  - %s\n" % p for p in problems).rstrip())

    if getattr(args, "verify_process", False):
        if not ctx["instance_id"] or not ctx["ssm_online"]:
            raise MeasurementError("cannot verify the process: the node is not reachable through SSM")

        output, _ = run_on_node(ctx["instance_id"], verify_script(), runner, sleep)
        matches = [line for line in (output or "").splitlines() if line.strip()]
        out("\nProcesses the freeze would match (read-only, no signal sent):")
        out("\n".join("  " + line for line in matches) if matches else "  none")
        out("  OK: exactly one process (the vLLM API server)" if len(matches) == 1
            else "  NOT OK: the freeze needs exactly one match; it would %s" % ("abort (nothing found)" if not matches else "stop only the first (lowest pid)"))

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure how the vLLM inference path recovers from a failure.")
    parser.add_argument("experiment", choices=["pod", "readiness", "node", "check"])
    parser.add_argument("--inject", action="store_true", help="cause the failure (asks you to type the target's name first)")
    parser.add_argument("--confirm", help="the target's name, instead of typing it (still must match)")
    parser.add_argument("--namespace", default=cs.NAMESPACE)
    parser.add_argument("--pod", help="use this vLLM pod")
    parser.add_argument("--instance-type", default=EXPECTED_INSTANCE_TYPE)
    parser.add_argument("--verify-process", action="store_true", help="check only: list the process the readiness freeze would stop (read-only, through SSM)")
    parser.add_argument("--no-inference", action="store_true", help="measure the infrastructure only; send no requests")
    parser.add_argument("--probes", type=int, default=5, help="most controlled requests while recovering (default 5)")
    parser.add_argument("--probe-interval", type=int, default=120, help="seconds between them (default 120)")
    parser.add_argument("--first-probe-delay", type=int, default=15, help="seconds after the failure before the first one (default 15)")
    parser.add_argument("--final-attempts", type=int, default=3, help="verification requests after recovery, stopping at the first success (default 3)")
    parser.add_argument("--final-retry-delay", type=int, default=10, help="seconds between them (default 10)")
    parser.add_argument("--max-freeze", type=int, default=900, help="readiness: resume after this many seconds even if not everything was seen")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--aws-interval", type=float, default=5, help="seconds between load balancer polls")
    parser.add_argument("--inject-timeout", type=int, default=600, help="without --inject: how long to wait for you to cause the failure")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    kube = cs.Kube(args.namespace)

    try:
        if args.experiment == "check":
            return check(kube, args)

        run = Run(args.experiment, args, kube)
        text = run.execute()

        if args.json:
            text = as_json(run.kind, run.tl, run.ctx, run.evidence, run.all_results(), run.notes, run.tl.get(START[run.kind]))

        print(text)

        return 0
    except MeasurementError as error:
        print("FAILED: %s" % error, file=sys.stderr)

        return 1
    except KeyboardInterrupt:
        print("interrupted (nothing is cleaned up automatically: see the printed resume command if a process was frozen)", file=sys.stderr)

        return 130


if __name__ == "__main__":
    sys.exit(main())
