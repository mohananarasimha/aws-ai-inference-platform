"""
scripts/measure_vllm_recovery.py against a scripted fake cluster with a fake clock.
No cluster, GPU or AWS needed; nothing here sleeps or starts a real thread.
"""

import json
import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measure_vllm_cold_start as cs  # noqa: E402
import measure_vllm_recovery as r  # noqa: E402
from test_measure_vllm_cold_start import BASE, at, make_claim, make_event, make_node, make_pod  # noqa: E402

SECRET = "SECRET-TOKEN-123"
T0 = 1000   # simulated seconds after BASE at which each run starts


# ---- builders ---------------------------------------------------------------------------------------------------

def pod_raw(name, created, ip, node="node-old", scheduled=None, started=None, ready=None, not_ready=None,
            deleting_at=None, restarts=0):
    raw = make_pod(created=created, name=name, node=node, scheduled=scheduled, running=started, ready=ready, restarts=restarts)
    raw["status"]["podIP"] = ip

    if not_ready is not None:
        raw["status"]["conditions"].append({"type": "Ready", "status": "False", "lastTransitionTime": at(not_ready)})

    if deleting_at is not None:
        raw["metadata"]["deletionTimestamp"] = at(deleting_at)
        raw["metadata"]["deletionGracePeriodSeconds"] = 30

    return raw


def gpu_node(name="node-old", ready=40, instance="i-0a1b2c3d", unknown=None, instance_type=None, workload="gpu"):
    raw = make_node(ready=ready, name=name)
    raw["metadata"]["labels"].update({"workload": workload, "karpenter.sh/nodepool": "gpu"})
    raw["spec"] = {"providerID": "aws:///us-east-1d/%s" % instance}

    if instance_type:
        raw["metadata"]["labels"]["node.kubernetes.io/instance-type"] = instance_type

    if unknown is not None:
        raw["status"]["conditions"] = [{"type": "Ready", "status": "Unknown", "lastTransitionTime": at(unknown)}]

    return raw


def daemonset_pod(node="node-old"):
    return {"metadata": {"name": "nvidia-device-plugin-x", "ownerReferences": [{"kind": "DaemonSet", "name": "ds"}]},
            "spec": {"nodeName": node}}


class Cluster:
    namespace = "default"

    def __init__(self):
        self.pod_list, self.node_list, self.claim_list = [], [], []
        self.ready_ips, self.unready_ips = [], []
        self.node_pod_list = None
        self.event_map, self.logs_by_pod, self.nlb = {}, {}, {}

    def get(self, args, namespace=True):
        kind = args[0]

        if kind == "pods" and "-A" in args:
            if self.node_pod_list is not None:
                return {"items": self.node_pod_list}

            return {"items": [p for p in self.pod_list if p["spec"].get("nodeName")] + [daemonset_pod()]}

        if kind == "pods":
            return {"items": list(self.pod_list)}

        if kind == "endpointslices":
            endpoints = [{"addresses": [ip], "conditions": {"ready": True}} for ip in self.ready_ips]
            endpoints += [{"addresses": [ip], "conditions": {"ready": False}} for ip in self.unready_ips]
            return {"items": [{"endpoints": endpoints}]}

        if kind == "nodes":
            return {"items": list(self.node_list)}

        raise AssertionError("unexpected get %s" % args)

    def pods(self):
        return list(self.pod_list)

    def nodeclaims(self):
        return list(self.claim_list)

    def node(self, name):
        return next((n for n in self.node_list if n["metadata"]["name"] == name), None)

    def events(self, name):
        return self.event_map.get(name, [])

    def logs(self, name):
        return self.logs_by_pod.get(name, "")


class FakeKube(Cluster):
    pass


class SyncThread:
    """Runs the probe immediately, so the fake clock cannot move underneath it."""

    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class Sim:
    def __init__(self, monkeypatch, start=T0):
        self.t = start
        self.actions = []
        monkeypatch.setattr(cs, "now_utc", lambda: BASE + timedelta(seconds=self.t))
        monkeypatch.setattr(r, "time", types.SimpleNamespace(monotonic=lambda: self.t, sleep=lambda s: None))
        monkeypatch.setattr(r, "threading", types.SimpleNamespace(Thread=SyncThread))

    def at(self, when, action):
        self.actions.append((when, action))

    def sleep(self, seconds):
        self.t += seconds

        for when, action in sorted(self.actions, key=lambda a: a[0]):
            if when <= self.t:
                self.actions.remove((when, action))
                action()


def make_args(**overrides):
    base = dict(inject=True, confirm=None, pod=None, instance_type="g5.xlarge", no_inference=False, probes=3,
                probe_interval=120, first_probe_delay=15, max_freeze=900, interval=3, aws_interval=5,
                inject_timeout=600, timeout=5000, namespace="default", json=False, final_attempts=3, final_retry_delay=10)
    base.update(overrides)

    return types.SimpleNamespace(**base)


def ec2_ok(instance_id="i-0a1b2c3d"):
    return lambda iid: {"id": iid, "state": "running", "type": "g5.xlarge", "terminated_at": None,
                        "tags": {"karpenter.sh/nodepool": "gpu", "karpenter.sh/nodeclaim": "gpu-xyz12"}}


def healthy_cluster():
    c = FakeKube()
    c.pod_list = [pod_raw("vllm-aaa-old", 0, "10.0.0.1", scheduled=50, started=60, ready=400)]
    c.node_list = [gpu_node()]
    c.claim_list = [make_claim(created=2, launched=5, registered=30, initialized=45, node="node-old")]
    c.ready_ips = ["10.0.0.1"]
    c.nlb = {"10.0.0.1": "healthy"}

    return c


def inference_caller(sim, works_from):
    def call(url, token):
        start = cs.now_utc()
        ok = sim.t >= works_from

        return {"start": start, "end": start + timedelta(seconds=2), "status": 200 if ok else 502, "exception": None,
                "error": None if ok else "vllm_unavailable",
                "problem": None if ok else "inference failed: HTTP 502 (vllm_unavailable)"}

    return call


def make_run(kind, cluster, sim, runner, args=None, call=None, ask=None, ec2=None, out=None):
    printed = out if out is not None else []

    return r.Run(kind, args or make_args(), cluster, nlb_states=lambda: dict(cluster.nlb),
                 gateway=lambda: ("https://api.example/invoke", SECRET), call=call or inference_caller(sim, 10**9),
                 runner=runner, sleep=sim.sleep, ask=ask or (lambda prompt: "unused"), out=printed.append,
                 ec2=ec2 or ec2_ok(), ssm=lambda iid: True), printed


# ---- pod failure end to end -----------------------------------------------------------------------------------------

def test_pod_failure_end_to_end(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old, commands = c.pod_list[0], []

    def runner(argv, timeout=60):
        commands.append(argv)
        old["metadata"]["deletionTimestamp"] = at(sim.t + 30)
        old["metadata"]["deletionGracePeriodSeconds"] = 30
        return ""

    def new(**stage):
        return pod_raw("vllm-bbb-new", 1001, "10.0.0.2", **stage)

    sim.at(1001, lambda: c.pod_list.append(new(node=None)))
    sim.at(1031, lambda: c.pod_list.remove(old))
    sim.at(1032, lambda: c.pod_list.__setitem__(0, new(scheduled=1032)))
    sim.at(1040, lambda: c.pod_list.__setitem__(0, new(scheduled=1032, started=1040)))
    sim.at(1330, lambda: (c.pod_list.__setitem__(0, new(scheduled=1032, started=1040, ready=1330)), setattr(c, "ready_ips", ["10.0.0.2"])))
    sim.at(1345, lambda: setattr(c, "nlb", {"10.0.0.2": "healthy"}))
    c.event_map["vllm-bbb-new"] = [make_event("Pulled", 1035, 'Container image "vllm/vllm-openai:v0.33.0" already present on machine')]
    c.logs_by_pod["vllm-bbb-new"] = "\n".join([
        at(1141)[:-1] + ".000000000Z INFO 11-11 05:19:01 [m.py] Loading weights took 105.06 seconds",
        at(1250)[:-1] + ".000000000Z INFO 11-11 05:20:50 [m.py] Model loading took 14.29 GiB memory and 216.169566 seconds",
        at(1327)[:-1] + ".500000000Z INFO:     Application startup complete."])

    run, printed = make_run("pod", c, sim, runner, ask=lambda prompt: "vllm-aaa-old", call=inference_caller(sim, 1346))
    text = run.execute()
    tl = run.tl

    assert commands == [["kubectl", "delete", "pod", "vllm-aaa-old", "-n", "default", "--wait=false"]]
    assert tl.get("P0") == BASE + timedelta(seconds=T0) and "script clock" in tl.source("P0")
    assert tl.get("P1") == BASE + timedelta(seconds=1001)
    assert tl.get("P2") == BASE + timedelta(seconds=1032)
    assert tl.get("P3") == BASE + timedelta(seconds=1040)
    assert tl.get("P4a") == BASE + timedelta(seconds=1330)
    assert tl.get("P4b") == BASE + timedelta(seconds=1327, milliseconds=500)
    assert 1345 <= (tl.get("P4c") - BASE).total_seconds() <= 1351

    values = dict(r.durations("pod", tl))
    assert values["replacement pod created"] == 1
    assert values["scheduling delay"] == 31
    assert values["container start to Ready (model startup)"] == 290
    assert 349 <= values["TOTAL pod-failure MTTR"] <= 358

    assert run.evidence["image_cached"] is True and run.evidence["same_node"] is True
    assert run.evidence["model"]["reused"] is False and 110 < run.evidence["model"]["download_seconds"] < 112

    assert "What the caller saw" in text and "HTTP 502 upstream error (vllm_unavailable)" in text
    assert "image                 already cached on the node" in text
    assert "model weights         downloaded again" in text
    assert SECRET not in text and SECRET not in "\n".join(printed)


def test_probes_are_few_and_spaced(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"]["deletionTimestamp"] = at(sim.t + 30)
        old["metadata"]["deletionGracePeriodSeconds"] = 30
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1400, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1400)),
                          c.pod_list.remove(old), setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    seen = []
    caller = inference_caller(sim, 1400)

    def counting(url, token):
        seen.append(sim.t)
        return caller(url, token)

    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=counting)
    run.execute()

    probes = run.prober.results
    assert len(probes) == 3                           # --probes 3, not one per poll
    assert [round((p["start"] - BASE).total_seconds()) for p in probes][:2] == [1015, 1135]   # first delay, then the interval
    assert len(seen) == 4                             # three probes plus one verification request (it succeeded first time)


def test_early_success_is_not_counted_as_recovery(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"]["deletionTimestamp"] = at(sim.t + 30)
        old["metadata"]["deletionGracePeriodSeconds"] = 30
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1300, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1300)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))

    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=inference_caller(sim, 0))   # the old pod still answers
    run.execute()

    assert any("succeeded before the replacement was ready" in n for n in run.notes)
    assert (run.tl.get("P5") - BASE).total_seconds() >= 1300


# ---- readiness failure ---------------------------------------------------------------------------------------------------

def ssm_runner(sim, calls):
    state = {"script": ""}

    def runner(argv, timeout=60):
        calls.append(argv)

        if argv[1] == "ssm" and argv[2] == "send-command":
            state["script"] = json.loads(argv[argv.index("--parameters") + 1])["commands"][0]
            return "cmd-1\n"

        if argv[1] == "ssm" and argv[2] == "get-command-invocation":
            output = "STOPPED 4242\n" if "kill -STOP" in state["script"] else "CONTINUED 4242\n"
            return json.dumps(["Success", output, cs.fmt_ts(cs.now_utc())])

        return ""

    return runner


def test_readiness_failure_end_to_end(monkeypatch):
    sim, c, calls = Sim(monkeypatch), healthy_cluster(), []
    pod = c.pod_list[0]

    sim.at(1022, lambda: setattr(c, "nlb", {"10.0.0.1": "unhealthy"}))
    sim.at(1610, lambda: (c.pod_list.__setitem__(0, pod_raw("vllm-aaa-old", 0, "10.0.0.1", scheduled=50, started=60, not_ready=1610)),
                          setattr(c, "ready_ips", []), setattr(c, "unready_ips", ["10.0.0.1"]), setattr(c, "nlb", {"10.0.0.1": "draining"})))
    sim.at(1625, lambda: (c.pod_list.__setitem__(0, pod_raw("vllm-aaa-old", 0, "10.0.0.1", scheduled=50, started=60, ready=1625)),
                          setattr(c, "ready_ips", ["10.0.0.1"]), setattr(c, "unready_ips", [])))
    sim.at(1640, lambda: setattr(c, "nlb", {"10.0.0.1": "healthy"}))

    def hung(url, token):
        start = cs.now_utc()
        ok = sim.t >= 1641

        return {"start": start, "end": start + timedelta(seconds=20 if not ok else 2), "status": 200 if ok else 504,
                "exception": None, "error": None if ok else "vllm_timeout",
                "problem": None if ok else "inference failed: HTTP 504 (vllm_timeout)"}

    run, printed = make_run("readiness", c, sim, ssm_runner(sim, calls), ask=lambda p: "vllm-aaa-old", call=hung)
    text = run.execute()
    tl = run.tl

    scripts = [json.loads(a[a.index("--parameters") + 1])["commands"][0] for a in calls if "send-command" in a]
    assert "kill -STOP" in scripts[0] and scripts[1] == 'kill -CONT 4242 && echo "CONTINUED 4242"'
    assert tl.get("R1") == BASE + timedelta(seconds=1610) and tl.source("R1") == "k8s"
    assert tl.get("R3") < tl.get("R1")                       # the load balancer noticed first
    assert "state=unhealthy" in tl.source("R3")
    assert tl.get("R4") > tl.get("R1") and tl.get("R4b") == BASE + timedelta(seconds=1625)

    values = dict(r.durations("readiness", tl))
    assert 600 <= values["kubelet readiness reaction (Ready=False)"] <= 612
    assert 18 <= values["NLB detection (target not healthy)"] <= 30
    assert values["outage window (injected to first inference)"] > values["recovery MTTR (resume to first inference)"]
    assert "HTTP 504 timeout (vllm_timeout)" in text
    assert "resume it with" in "\n".join(printed)            # the emergency command is printed before waiting
    assert run.frozen_pid is None


def test_readiness_without_inject_is_refused(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    run, _ = make_run("readiness", c, sim, lambda a, t=60: "", args=make_args(inject=False))

    with pytest.raises(r.MeasurementError, match="needs --inject"):
        run.execute()


def test_a_failed_run_never_leaves_the_process_frozen(monkeypatch):
    sim, c, calls = Sim(monkeypatch), healthy_cluster(), []
    run, printed = make_run("readiness", c, sim, ssm_runner(sim, calls), ask=lambda p: "vllm-aaa-old",
                            args=make_args(timeout=50, no_inference=True))
    c.nlb = {"10.0.0.1": "healthy"}

    with pytest.raises(r.MeasurementError, match="no recovery within"):
        run.execute()

    scripts = [json.loads(a[a.index("--parameters") + 1])["commands"][0] for a in calls if "send-command" in a]
    assert any("kill -CONT 4242" in s for s in scripts)
    assert any("was resumed" in line for line in printed)


def test_readiness_that_never_fails_is_reported_as_never_lost(monkeypatch):
    sim, c, calls = Sim(monkeypatch), healthy_cluster(), []
    run, _ = make_run("readiness", c, sim, ssm_runner(sim, calls), ask=lambda p: "vllm-aaa-old",
                      args=make_args(max_freeze=30, no_inference=True))
    run.execute()

    assert run.tl.source("R4b") == "never lost" and run.tl.source("R6") == "never lost"
    assert any("Resumed after --max-freeze" in n for n in run.notes)


# ---- node failure end to end -------------------------------------------------------------------------------------------------

def test_node_failure_end_to_end(monkeypatch):
    sim, c, commands = Sim(monkeypatch), healthy_cluster(), []

    def runner(argv, timeout=60):
        commands.append(argv)
        return ""

    new_claim = lambda **kw: make_claim(created=1102, launched=1105, registered=1130, name="gpu-new", node=kw.get("node"), initialized=kw.get("initialized"))

    sim.at(1040, lambda: c.node_list.__setitem__(0, gpu_node(unknown=1040)))
    sim.at(1100, lambda: (c.node_list.clear(), c.pod_list.clear()))
    sim.at(1101, lambda: c.pod_list.append(pod_raw("vllm-new-pod", 1101, "10.0.0.7", node=None)))
    sim.at(1102, lambda: c.claim_list.append(new_claim(node=None)))
    sim.at(1135, lambda: (c.node_list.append(gpu_node(name="node-new", ready=1135, instance="i-0f1e2d3c")),
                          c.claim_list.__setitem__(1, new_claim(node="node-new", initialized=1160))))
    sim.at(1162, lambda: c.pod_list.__setitem__(0, pod_raw("vllm-new-pod", 1101, "10.0.0.7", node="node-new", scheduled=1162)))
    sim.at(1470, lambda: c.pod_list.__setitem__(0, pod_raw("vllm-new-pod", 1101, "10.0.0.7", node="node-new", scheduled=1162, started=1470)))
    sim.at(1800, lambda: c.pod_list.__setitem__(0, pod_raw("vllm-new-pod", 1101, "10.0.0.7", node="node-new", scheduled=1162, started=1470, ready=1800)))
    sim.at(1815, lambda: setattr(c, "nlb", {"10.0.0.7": "healthy"}))
    c.event_map["vllm-new-pod"] = [make_event("Pulling", 1165), make_event("Pulled", 1465, 'Successfully pulled image "x" in 4m58s (4m58s including waiting)')]
    c.logs_by_pod["vllm-new-pod"] = ""

    run, printed = make_run("node", c, sim, runner, ask=lambda p: "i-0a1b2c3d", call=inference_caller(sim, 1816))
    text = run.execute()
    tl = run.tl

    assert commands == [["aws", "ec2", "terminate-instances", "--instance-ids", "i-0a1b2c3d"]]
    assert tl.get("N1") == BASE + timedelta(seconds=1040) and "Ready condition" in tl.source("N1")
    assert tl.get("N2") == BASE + timedelta(seconds=1101)
    assert tl.get("N3") == BASE + timedelta(seconds=1102)            # the new claim, not the old one
    assert tl.get("N4") == BASE + timedelta(seconds=1135)
    assert tl.get("N5") == BASE + timedelta(seconds=1160)
    assert tl.get("N6") == BASE + timedelta(seconds=1470) and tl.get("N7") == BASE + timedelta(seconds=1800)

    values = dict(r.durations("node", tl))
    assert values["node-failure detection"] == 40
    assert values["Karpenter reaction (pod Pending to NodeClaim)"] == 1
    assert values["new GPU node provisioning"] == 33
    assert values["model initialization"] == 330
    assert run.evidence["same_node"] is False and run.evidence["image_cached"] is False
    assert "Against the cold-start baseline" in text and "pulled again" in text


def test_node_removed_before_it_was_seen_unready_is_still_detected(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    run, _ = make_run("node", c, sim, lambda a, t=60: "", ask=lambda p: "i-0a1b2c3d", args=make_args(no_inference=True, timeout=60))
    sim.at(1010, lambda: (c.node_list.clear(), c.pod_list.clear()))

    with pytest.raises(r.MeasurementError):
        run.execute()

    assert run.tl.source("N1") == "observed (node object removed)"


def test_the_old_claim_is_never_taken_for_the_replacement():
    tl = r.Timeline()
    tl.set("N0", BASE + timedelta(seconds=T0), "t")
    ctx = {"node": {"name": "node-old"}, "claim": {"name": "gpu-xyz12"}, "pod": {"name": "vllm-aaa-old", "created": BASE}}
    old_claim = cs.nodeclaim_info(make_claim(created=2, node="node-old"))
    snap = {"now": BASE + timedelta(seconds=T0 + 5), "pods": [], "claims": [old_claim], "nodes": {}, "eps_ready": set(), "nlb": None}

    r.update_node(tl, ctx, snap)

    assert tl.get("N3") is None and tl.get("N4") is None


def test_missing_objects_do_not_crash_the_updaters():
    snap = {"now": BASE, "pods": [], "claims": [], "nodes": {}, "eps_ready": set(), "nlb": None}
    pod_ctx = {"pod": {"name": "p", "created": BASE, "ip": "1.1.1.1"}, "claim": None, "node": None}

    for update in (r.update_pod, r.update_readiness, r.update_node):
        update(r.Timeline(), pod_ctx, snap)

    tl = r.Timeline()
    tl.set("N0", BASE, "t")
    r.update_node(tl, {"pod": {"name": "p"}, "claim": {"name": "c"}, "node": {"name": "n"}}, snap)
    assert tl.get("N1") == BASE


# ---- observing a failure you cause yourself ------------------------------------------------------------------------------------

def test_pod_failure_started_by_hand_takes_its_start_from_kubernetes(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]
    sim.at(1010, lambda: old["metadata"].update({"deletionTimestamp": at(1040), "deletionGracePeriodSeconds": 30}))
    run, printed = make_run("pod", c, sim, lambda a, t=60: pytest.fail("must not inject"), args=make_args(inject=False, no_inference=True, timeout=30))

    with pytest.raises(r.MeasurementError):
        run.execute()

    assert run.tl.get("P0") == BASE + timedelta(seconds=1010) and "deletionTimestamp" in run.tl.source("P0")
    assert any("kubectl delete pod vllm-aaa-old" in line for line in printed)


def test_node_failure_started_by_hand_uses_the_ec2_termination_time(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    states = {"state": "running"}

    def ec2(iid):
        return {"id": iid, "state": states["state"], "type": "g5.xlarge", "tags": {"karpenter.sh/nodepool": "gpu", "karpenter.sh/nodeclaim": "gpu-xyz12"},
                "terminated_at": BASE + timedelta(seconds=1005) if states["state"] != "running" else None}

    sim.at(1010, lambda: states.update(state="shutting-down"))
    run, _ = make_run("node", c, sim, lambda a, t=60: pytest.fail("must not inject"), ec2=ec2, args=make_args(inject=False, no_inference=True, timeout=20))

    with pytest.raises(r.MeasurementError):
        run.execute()

    assert run.tl.get("N0") == BASE + timedelta(seconds=1005) and run.tl.source("N0") == "EC2 StateTransitionReason"


def test_no_failure_within_the_wait_is_an_error(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    run, _ = make_run("pod", c, sim, lambda a, t=60: "", args=make_args(inject=False, inject_timeout=20))

    with pytest.raises(r.MeasurementError, match="no failure was injected"):
        run.execute()


def test_a_run_that_never_recovers_fails_with_the_last_milestone(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", args=make_args(no_inference=True, timeout=120))

    with pytest.raises(r.MeasurementError, match=r"no recovery within 120s \(stuck after P1\)"):
        run.execute()


# ---- safety ---------------------------------------------------------------------------------------------------------------------

def context(cluster=None, **overrides):
    c = cluster or healthy_cluster()
    ctx = r.identify(c, None, ec2_ok(), lambda iid: True)
    ctx.update(overrides)

    return ctx


def test_the_expected_target_passes_every_check():
    ctx = context()

    assert ctx["instance_id"] == "i-0a1b2c3d"
    for kind in ("pod", "readiness", "node"):
        assert r.safety_problems(kind, ctx) == []


def test_wrong_instance_type_node_is_blocked():
    c = healthy_cluster()
    c.node_list = [gpu_node(instance_type="p4d.24xlarge")]

    assert any("expected g5.xlarge" in p for p in r.safety_problems("node", context(c)))


def test_a_node_that_is_not_a_gpu_node_is_blocked():
    c = healthy_cluster()
    c.node_list = [gpu_node(workload="system")]

    assert any("not a Karpenter GPU node" in p for p in r.safety_problems("node", context(c)))


def test_unrelated_workloads_on_the_node_block_injection():
    c = healthy_cluster()
    c.node_pod_list = [c.pod_list[0], {"metadata": {"name": "someone-elses-db", "ownerReferences": [{"kind": "ReplicaSet"}]}, "spec": {"nodeName": "node-old"}}, daemonset_pod()]

    problems = r.safety_problems("node", context(c))

    assert any("someone-elses-db" in p for p in problems)
    assert not any("nvidia-device-plugin" in p for p in problems)      # daemonset pods do not count


def test_two_gpu_nodes_block_node_injection():
    c = healthy_cluster()
    c.node_list.append(gpu_node(name="node-2", instance="i-0b2b2b2b"))

    assert any("exactly one GPU node" in p for p in r.safety_problems("node", context(c)))


def test_ec2_tags_must_match_the_nodeclaim():
    c = healthy_cluster()
    ctx = r.identify(c, None, lambda iid: {"id": iid, "state": "running", "type": "g5.xlarge", "terminated_at": None,
                                           "tags": {"karpenter.sh/nodepool": "gpu", "karpenter.sh/nodeclaim": "gpu-someone-else"}}, lambda i: True)

    assert any("NodeClaim tag does not match" in p for p in r.safety_problems("node", ctx))


def test_a_pod_that_is_not_ready_or_is_deleting_is_blocked():
    c = healthy_cluster()
    c.pod_list = [pod_raw("vllm-aaa-old", 0, "10.0.0.1", scheduled=50, started=60)]

    assert any("not Ready" in p for p in r.safety_problems("pod", context(c)))


def test_readiness_needs_ssm_online():
    assert any("SSM" in p for p in r.safety_problems("readiness", context(ssm_online=False)))


def test_injection_is_refused_and_nothing_runs_when_a_check_fails(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    c.node_list = [gpu_node(instance_type="p4d.24xlarge")]
    run, _ = make_run("node", c, sim, lambda a, t=60: pytest.fail("must not run"), ask=lambda p: "i-0a1b2c3d")

    with pytest.raises(r.MeasurementError, match="safety checks failed"):
        run.execute()


@pytest.mark.parametrize("kind, typed", [("pod", "wrong-name"), ("node", "vllm-aaa-old"), ("node", ""), ("readiness", "i-0a1b2c3d")])
def test_a_wrong_confirmation_cancels_before_anything_runs(monkeypatch, kind, typed):
    sim, c = Sim(monkeypatch), healthy_cluster()
    run, printed = make_run(kind, c, sim, lambda a, t=60: pytest.fail("must not run"), ask=lambda p: typed)

    with pytest.raises(r.MeasurementError, match="cancelled"):
        run.execute()

    assert any("About to run" in line for line in printed)         # the target and command were shown first


def test_confirm_flag_must_match_too(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    run, _ = make_run("pod", c, sim, lambda a, t=60: pytest.fail("must not run"), args=make_args(confirm="something-else"))

    with pytest.raises(r.MeasurementError, match="cancelled"):
        run.execute()


def test_confirmation_helper():
    assert r.confirmation_ok("x", "x") and r.confirmation_ok("x", None, lambda p: " x ")
    assert not r.confirmation_ok("x", "y") and not r.confirmation_ok("x", None, lambda p: "")


def test_shell_snippets_reject_unsafe_input():
    assert "pgrep -f '[Q]wen2.5-7B-Instruct'" in r.freeze_script()
    assert r.resume_script(4242) == 'kill -CONT 4242 && echo "CONTINUED 4242"'

    with pytest.raises(r.MeasurementError):
        r.freeze_script("x'; rm -rf / #")

    for bad in ("4242; reboot", "", "$(id)"):
        with pytest.raises(r.MeasurementError):
            r.resume_script(bad)


def test_the_node_command_names_exactly_one_instance():
    ctx = context()

    assert r.node_inject_command(ctx) == ["aws", "ec2", "terminate-instances", "--instance-ids", "i-0a1b2c3d"]
    assert r.pod_inject_command(ctx) == ["kubectl", "delete", "pod", "vllm-aaa-old", "-n", "default", "--wait=false"]


def test_ssm_command_runs_on_one_instance_only():
    command = r.ssm_command("i-0a1b2c3d", "echo hi")

    assert command[command.index("--instance-ids") + 1] == "i-0a1b2c3d"
    assert "AWS-RunShellScript" in command


# ---- parsing helpers ------------------------------------------------------------------------------------------------------------------

def test_instance_id_comes_from_the_provider_id():
    assert r.instance_id_from("aws:///us-east-1d/i-0123456789abcdef0") == "i-0123456789abcdef0"
    assert r.instance_id_from("aws:///us-east-1d/not-an-instance") is None
    assert r.instance_id_from(None) is None


def test_ec2_instance_reads_the_termination_time(monkeypatch):
    payload = {"Reservations": [{"Instances": [{"State": {"Name": "shutting-down"}, "InstanceType": "g5.xlarge",
                                                  "StateTransitionReason": "User initiated (2026-11-11 05:40:12 GMT)",
                                                  "Tags": [{"Key": "karpenter.sh/nodepool", "Value": "gpu"}]}]}]}
    monkeypatch.setattr(r, "run_command", lambda argv, timeout=60: json.dumps(payload))

    info = r.ec2_instance("i-0abc")

    assert info["state"] == "shutting-down" and info["tags"]["karpenter.sh/nodepool"] == "gpu"
    assert info["terminated_at"] == BASE + timedelta(minutes=40, seconds=12)


def test_model_cache_reading():
    real = "Loading weights took 105.06 seconds\nModel loading took 14.29 GiB memory and 216.169566 seconds"
    cached = "Loading weights took 4.1 seconds\nModel loading took 14.29 GiB memory and 6.0 seconds"

    assert r.model_cache(real)["reused"] is False and round(r.model_cache(real)["download_seconds"]) == 111
    assert r.model_cache(cached)["reused"] is True
    assert r.model_cache("Time spent downloading weights for Qwen: 42.5 seconds")["download_seconds"] == 42.5
    assert r.model_cache("nothing useful")["reused"] is None and r.model_cache("")["reused"] is None


def test_replacement_pod_selection():
    old = r.pod_state(pod_raw("old", 0, "1", ready=10))
    older = r.pod_state(pod_raw("older-leftover", 5, "2"))
    dying = r.pod_state(pod_raw("dying", 1001, "3", deleting_at=1030))
    new = r.pod_state(pod_raw("new", 1002, "4"))
    failure = BASE + timedelta(seconds=T0, milliseconds=500)

    assert r.replacement_pod([old, older, dying, new], "old", failure)["name"] == "new"
    assert r.replacement_pod([old], "old", failure) is None
    assert r.replacement_pod([old, older], "old", failure) is None


def test_pod_state_derives_when_the_delete_was_requested():
    state = r.pod_state(pod_raw("p", 0, "1", ready=10, deleting_at=1030))

    assert state["deleting"] and state["deletion_requested_at"] == BASE + timedelta(seconds=1000)
    assert r.pod_state(pod_raw("p", 0, "1", not_ready=500))["not_ready_at"] == BASE + timedelta(seconds=500)


# ---- the user's view, the report, secrets --------------------------------------------------------------------------------------------------

def test_how_failures_look_to_the_caller():
    ok = {"problem": None}

    assert r.describe_result(ok) == "success"
    assert r.describe_result({"problem": "x", "status": 504, "error": "vllm_timeout"}) == "HTTP 504 timeout (vllm_timeout)"
    assert r.describe_result({"problem": "x", "status": 502, "error": "vllm_unavailable"}) == "HTTP 502 upstream error (vllm_unavailable)"
    assert r.describe_result({"problem": "x", "status": 503, "error": "vllm_endpoint_not_found"}) == "HTTP 503 endpoint not found (vllm_endpoint_not_found)"
    assert r.describe_result({"problem": "x", "status": None, "exception": "TimeoutError", "error": None}) == "no HTTP response (TimeoutError)"


def test_prober_never_exceeds_its_limit_or_overlaps(monkeypatch):
    monkeypatch.setattr(r, "threading", types.SimpleNamespace(Thread=SyncThread))
    calls = []
    prober = r.Prober(lambda: calls.append(1) or {"start": BASE, "end": BASE, "problem": "x", "status": 502, "error": None, "exception": None},
                      first_delay=10, interval=60, maximum=2, clock=None)
    start = BASE

    for second in range(0, 400):
        prober.tick(start + timedelta(seconds=second), start)

    assert len(calls) == 2
    prober.tick(start + timedelta(seconds=9999), None)          # no window yet: nothing sent
    assert len(calls) == 2


def test_no_inference_sends_no_requests(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1100, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1100)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", args=make_args(no_inference=True),
                      call=lambda u, t: pytest.fail("must not call"))
    run.gateway = lambda: pytest.fail("must not fetch credentials")
    text = run.execute()

    assert "no request sent" in text and run.prober is None


def test_secrets_never_reach_the_json_or_the_text(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1100, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1100)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    run, printed = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=inference_caller(sim, 1101))
    text = run.execute()
    as_text = r.as_json(run.kind, run.tl, run.ctx, run.evidence, run.prober.results, run.notes, run.tl.get("P0"))

    assert json.loads(as_text)["experiment"] == "pod"
    for blob in (text, as_text, "\n".join(printed)):
        assert SECRET not in blob and "Bearer" not in blob and "token" not in blob.lower().replace("tokens", "")


def test_report_has_every_section_and_no_none(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1100, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1100)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=inference_caller(sim, 1101))
    text = run.execute()

    for needle in ("vLLM Failure Recovery: pod failure", "Target (before the failure)", "Timeline", "Durations", "Recovery evidence",
                   "What the caller saw", "P0", "P5", "TOTAL pod-failure MTTR", "1 s resolution"):
        assert needle in text

    assert "None" not in text


def test_check_reports_each_experiment_and_its_command(capsys):
    printed = []
    c = healthy_cluster()
    monkey_ec2, monkey_ssm = ec2_ok(), lambda iid: True
    original = r.identify
    r.identify = lambda kube, pod, ec2=monkey_ec2, ssm=monkey_ssm: original(kube, pod, monkey_ec2, monkey_ssm)

    try:
        assert r.check(c, make_args(), out=printed.append) == 0
    finally:
        r.identify = original

    text = "\n".join(printed)
    assert "pod: OK to run" in text and "readiness: OK to run" in text and "node: OK to run" in text
    assert "kubectl delete pod vllm-aaa-old" in text and "terminate-instances --instance-ids i-0a1b2c3d" in text
    assert "type to confirm: i-0a1b2c3d" in text


def test_a_failed_first_verification_request_is_retried_and_reported(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1100, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1100)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=inference_caller(sim, 1118), args=make_args(probes=0))
    text = run.execute()

    assert len(run.final_results) >= 2 and run.final_results[0]["problem"] and run.final_results[-1]["problem"] is None
    assert any("healthy before traffic was served" in n for n in run.notes)
    assert "HTTP 502 upstream error" in text


def test_the_verification_request_gives_up_after_its_attempts(monkeypatch):
    sim, c = Sim(monkeypatch), healthy_cluster()
    old = c.pod_list[0]

    def runner(argv, timeout=60):
        old["metadata"].update({"deletionTimestamp": at(sim.t + 30), "deletionGracePeriodSeconds": 30})
        return ""

    sim.at(1001, lambda: c.pod_list.append(pod_raw("vllm-bbb-new", 1001, "10.0.0.2", node=None)))
    sim.at(1100, lambda: (c.pod_list.__setitem__(1, pod_raw("vllm-bbb-new", 1001, "10.0.0.2", scheduled=1032, started=1040, ready=1100)),
                          setattr(c, "nlb", {"10.0.0.2": "healthy"})))
    run, _ = make_run("pod", c, sim, runner, ask=lambda p: "vllm-aaa-old", call=inference_caller(sim, 10**9), args=make_args(probes=0, final_attempts=2))

    with pytest.raises(r.MeasurementError, match=r"final inference failed after recovery \(2 attempt"):
        run.execute()


def test_verify_process_is_read_only_and_reports_the_match():
    c = healthy_cluster()
    sent = []

    def runner(argv, timeout=60):
        sent.append(argv)

        if argv[2] == "send-command":
            return "cmd-1\n"

        return json.dumps(["Success", "4242 python3 -m vllm.entrypoints.openai.api_server Qwen/Qwen2.5-7B-Instruct\n", "2026-11-11T05:00:00.000Z"])

    printed = []
    args = make_args(verify_process=True)
    original = r.identify
    r.identify = lambda kube, pod, ec2=None, ssm=None: original(kube, pod, ec2_ok(), lambda i: True)

    try:
        r.check(c, args, out=printed.append, runner=runner, sleep=lambda s: None)
    finally:
        r.identify = original

    script = json.loads(sent[0][sent[0].index("--parameters") + 1])["commands"][0]
    assert script == "pgrep -af '[Q]wen2.5-7B-Instruct' | cut -c1-160"
    assert "kill" not in script
    assert any("OK: exactly one process" in line for line in printed)


def test_verify_process_flags_zero_or_many_matches():
    for output, expected in (("", "abort (nothing found)"), ("1 a\n2 b\n", "stop only the first")):
        c = healthy_cluster()
        printed = []

        def runner(argv, timeout=60, output=output):
            return "cmd-1\n" if argv[2] == "send-command" else json.dumps(["Success", output, "2026-11-11T05:00:00.000Z"])

        original = r.identify
        r.identify = lambda kube, pod, ec2=None, ssm=None: original(kube, pod, ec2_ok(), lambda i: True)

        try:
            r.check(c, make_args(verify_process=True), out=printed.append, runner=runner, sleep=lambda s: None)
        finally:
            r.identify = original

        assert any(expected in line for line in printed)
