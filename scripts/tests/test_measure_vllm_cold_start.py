"""
Timestamp parsing and cold-start arithmetic for scripts/measure_vllm_cold_start.py.
Everything here runs on synthetic Kubernetes JSON: no cluster, GPU or AWS needed.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measure_vllm_cold_start as m  # noqa: E402

BASE = datetime(2026, 11, 11, 5, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_pod(created=0, name="vllm-abc-1", owner="ReplicaSet", node="node-gpu", running=None,
             ready=None, scheduled=None, unschedulable=None, restarts=0, waiting=None, deleting=False):
    conditions = []

    if unschedulable is not None:
        conditions.append({"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                           "lastTransitionTime": at(unschedulable)})

    if scheduled is not None:
        conditions.append({"type": "PodScheduled", "status": "True", "lastTransitionTime": at(scheduled)})

    if ready is not None:
        conditions.append({"type": "Ready", "status": "True", "lastTransitionTime": at(ready)})

    state = {}

    if running is not None:
        state = {"running": {"startedAt": at(running)}}
    elif waiting:
        state = {"waiting": {"reason": waiting}}

    meta = {"name": name, "creationTimestamp": at(created), "labels": {"app": "vllm"},
            "ownerReferences": [{"kind": owner, "name": "vllm-abc"}] if owner else []}

    if deleting:
        meta["deletionTimestamp"] = at(created + 1)

    return {
        "metadata": meta,
        "spec": {"nodeName": node, "containers": [{"name": "vllm", "image": "vllm/vllm-openai:v0.33.0"}]},
        "status": {"phase": "Running", "podIP": "10.0.0.9", "conditions": conditions,
                   "containerStatuses": [{"name": "vllm", "restartCount": restarts, "state": state,
                                          "image": "vllm/vllm-openai:v0.33.0"}]},
    }


def make_claim(created=2, launched=5, registered=30, initialized=45, node="node-gpu", name="gpu-xyz12"):
    def cond(kind, when):
        return {"type": kind, "status": "True" if when is not None else "Unknown",
                "lastTransitionTime": at(when if when is not None else created)}

    return {"metadata": {"name": name, "creationTimestamp": at(created),
                         "labels": {"node.kubernetes.io/instance-type": "g5.xlarge",
                                    "topology.kubernetes.io/zone": "us-east-1d"}},
            "status": {"nodeName": node, "conditions": [cond("Launched", launched), cond("Registered", registered),
                                                        cond("Initialized", initialized)]}}


def make_node(ready=40, name="node-gpu", gpu="1"):
    return {"metadata": {"name": name, "labels": {"node.kubernetes.io/instance-type": "g5.xlarge",
                                                  "topology.kubernetes.io/zone": "us-east-1d"}},
            "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": at(ready)}],
                       "allocatable": {"nvidia.com/gpu": gpu} if gpu else {}}}


def make_event(reason, when, message="", kind="Normal"):
    return {"reason": reason, "firstTimestamp": at(when), "message": message, "type": kind}


# ---- time parsing ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-11-11T05:00:00Z", BASE),
    ("2026-11-11T05:00:00.250000Z", BASE + timedelta(milliseconds=250)),
    ("2026-11-11T05:00:00.123456789Z", BASE + timedelta(microseconds=123456)),
    ("2026-11-11T07:00:00+02:00", BASE),
    ("2026-11-11T05:00:00", BASE),
])
def test_parse_ts(text, expected):
    assert m.parse_ts(text) == expected


@pytest.mark.parametrize("bad", [None, "", "not a time", "2026-13-45T99:00:00Z"])
def test_parse_ts_rejects_garbage(bad):
    assert m.parse_ts(bad) is None


def test_seconds_and_formatting():
    assert m.seconds_between(BASE + timedelta(seconds=75), BASE) == 75
    assert m.seconds_between(None, BASE) is None
    assert m.fmt_secs(75) == "75s"
    assert m.fmt_secs(2.4) == "2.4s"
    assert m.fmt_secs(None) == "n/a"
    assert m.fmt_ts(BASE) == "2026-11-11T05:00:00Z"
    assert m.fmt_ts(BASE + timedelta(milliseconds=500)) == "2026-11-11T05:00:00.500Z"
    assert m.fmt_ts(None) == "n/a"


def test_parse_duration():
    assert m.parse_duration("1m23.5s") == 83.5
    assert m.parse_duration("2h1m0s") == 7260
    assert m.parse_duration("45s") == 45
    assert m.parse_duration("") == 0


# ---- choosing the right pod / NodeClaim ----------------------------------------------------------

def test_picks_the_newest_deployment_pod():
    old, new = make_pod(created=0, name="vllm-old"), make_pod(created=100, name="vllm-new")

    chosen, warnings = m.select_vllm_pod([old, new])

    assert chosen["metadata"]["name"] == "vllm-new"
    assert "2 vLLM pods" in warnings[0]


def test_ignores_pods_without_a_replicaset_owner_and_deleting_pods():
    bare = make_pod(created=200, name="vllm", owner=None)
    deleting = make_pod(created=150, name="vllm-dying", deleting=True)
    real = make_pod(created=100, name="vllm-real")

    chosen, warnings = m.select_vllm_pod([bare, deleting, real])

    assert chosen["metadata"]["name"] == "vllm-real"
    assert warnings == []


def test_no_pod_found():
    assert m.select_vllm_pod([])[0] is None
    assert m.select_vllm_pod([make_pod(owner=None)])[0] is None


def test_explicit_pod_name_wins_even_without_owner():
    chosen, _ = m.select_vllm_pod([make_pod(name="vllm", owner=None), make_pod(name="vllm-x")], name="vllm")

    assert chosen["metadata"]["name"] == "vllm"


def test_nodeclaim_is_matched_by_the_pods_node_not_the_first_one():
    other = make_claim(name="gpu-other", node="some-other-node")
    mine = make_claim(name="gpu-mine", node="node-gpu")

    assert m.find_nodeclaim([other, mine], "node-gpu", None)["metadata"]["name"] == "gpu-mine"


def test_before_binding_the_nominated_nodeclaim_is_used():
    claims = [make_claim(name="gpu-a", node=None), make_claim(name="gpu-b", node=None)]

    assert m.find_nodeclaim(claims, None, "gpu-b")["metadata"]["name"] == "gpu-b"
    assert m.find_nodeclaim(claims, None, None) is None


# ---- events ----------------------------------------------------------------------------------------

def test_events_info():
    info = m.events_info([
        make_event("FailedScheduling", 1, "0/1 nodes are available", "Warning"),
        make_event("Nominated", 2, "Pod should schedule on: nodeclaim/gpu-7cf6r"),
        make_event("Pulling", 50, "Pulling image"),
        make_event("Pulled", 170, 'Successfully pulled image "x" in 2m0.5s (2m0.5s including waiting)'),
        make_event("BackOff", 180, "Back-off restarting failed container", "Warning"),
    ])

    assert info["nominated"] == "gpu-7cf6r"
    assert info["failed_scheduling"] == 1
    assert m.seconds_between(info["pulled_at"], info["pulling_at"]) == 120
    assert info["pull_seconds"] == 120.5
    assert info["warnings"] and "Back-off" in info["warnings"][0]


# ---- the headline arithmetic ------------------------------------------------------------------------

def scenario(**overrides):
    pod = m.pod_info(make_pod(created=0, node="node-gpu", unschedulable=1, scheduled=50, running=200, ready=310))
    claim = m.nodeclaim_info(make_claim(created=2, launched=5, registered=30, initialized=45))
    node = m.node_info(make_node(ready=40))
    health = BASE + timedelta(seconds=overrides.get("health", 305))
    inference = BASE + timedelta(seconds=overrides["inference"]) if "inference" in overrides else None

    return pod, claim, node, m.milestones(pod, claim, node, health, inference, plugin_ready=BASE + timedelta(seconds=44))


def test_milestones_come_from_the_right_sources():
    pod, claim, node, ms = scenario(inference=315)

    assert ms["T0"] == BASE
    assert ms["T1"] == BASE + timedelta(seconds=2)
    assert ms["T2"] == BASE + timedelta(seconds=40)
    assert ms["T2b"] == BASE + timedelta(seconds=45)
    assert ms["T3"] == BASE + timedelta(seconds=200)
    assert ms["T4a"] == BASE + timedelta(seconds=310)
    assert ms["T4b"] == BASE + timedelta(seconds=305)
    assert ms["T5"] == BASE + timedelta(seconds=315)


def test_durations_match_the_spec():
    _, _, _, ms = scenario(inference=315)
    steps, summary = m.durations(ms)

    assert dict(steps) == {"Karpenter reaction": 2, "Node provisioning": 38, "Container scheduling": 160,
                           "Model initialization": 110, "First request": 5}
    assert dict(summary) == {"GPU capacity ready": 40, "Model cold start": 110, "Total usable cold start": 315}


def test_without_inference_the_total_is_pod_ready():
    _, _, _, ms = scenario()
    _, summary = m.durations(ms)

    assert dict(summary)["Pod ready (no inference)"] == 310
    assert "Total usable cold start" not in dict(summary)


def test_effective_ready_is_the_later_of_pod_ready_and_health():
    early, late = BASE, BASE + timedelta(seconds=10)

    assert m.effective_ready(early, late) == late
    assert m.effective_ready(late, early) == late
    assert m.effective_ready(early, None) == early
    assert m.effective_ready(None, None) is None


def test_t2b_falls_back_to_when_the_script_saw_the_gpu():
    pod = m.pod_info(make_pod())
    claim = m.nodeclaim_info(make_claim(initialized=None))
    seen = BASE + timedelta(seconds=47)

    ms = m.milestones(pod, claim, m.node_info(make_node()), None, None, gpu_seen=seen)

    assert ms["T2b"] == seen


def test_breakdown_explains_the_container_scheduling_gap():
    pod, claim, node, ms = scenario()
    events = m.events_info([make_event("Pulling", 52), make_event("Pulled", 190, 'pulled in 2m18s')])

    rows = {name: value for name, value, _ in m.breakdown(ms, pod, claim, events, None, None)}

    assert rows["image pull"] == 138
    assert rows["EC2 instance launched"] == 3
    assert rows["node registered with the cluster"] == 25
    assert rows["pod bound to the GPU node"] == 10
    assert rows["pod Ready after /health first passed"] == 5


# ---- warnings and failures ----------------------------------------------------------------------------

def test_warm_node_is_not_reported_as_a_cold_start():
    pod = m.pod_info(make_pod(created=100))
    claim = m.nodeclaim_info(make_claim(created=10))
    ms = m.milestones(pod, claim, None, None, None)

    assert any("warm start" in w for w in m.warnings_for(pod, claim, ms, posthoc=False))


def test_restarts_and_posthoc_are_flagged():
    pod = m.pod_info(make_pod(restarts=2, ready=300))
    ms = m.milestones(pod, None, None, None, None)

    notes = m.warnings_for(pod, None, ms, posthoc=True)

    assert any("restarted 2" in n for n in notes)
    assert any("already Ready" in n for n in notes)


def test_negative_durations_are_flagged():
    pod = m.pod_info(make_pod(created=50))
    claim = m.nodeclaim_info(make_claim(created=10))
    ms = m.milestones(pod, claim, None, None, None)

    assert any("T0->T1 is negative" in n for n in m.warnings_for(pod, claim, ms, posthoc=False))


class Args:
    nodeclaim_timeout = 180


def test_fatal_container_states_fail_fast():
    pod = m.pod_info(make_pod(node="n", waiting="ImagePullBackOff"))
    events = m.events_info([make_event("Failed", 5, "Failed to pull image x", "Warning")])

    with pytest.raises(m.MeasurementError, match="ImagePullBackOff"):
        m.check_failures(pod, events, None, 10, Args())


def test_no_nodeclaim_after_the_timeout_fails_clearly():
    pod = m.pod_info(make_pod(node=None))
    events = m.events_info([make_event("FailedScheduling", 1, "0/1 nodes", "Warning")])

    m.check_failures(pod, events, None, 60, Args())

    with pytest.raises(m.MeasurementError, match="no Karpenter NodeClaim"):
        m.check_failures(pod, events, None, 181, Args())


def test_a_nodeclaim_resets_the_no_claim_failure():
    pod = m.pod_info(make_pod(node=None))
    events = m.events_info([])

    m.check_failures(pod, events, m.nodeclaim_info(make_claim()), 999, Args())


def test_partial_progress_lists_what_was_reached():
    pod = m.pod_info(make_pod(created=0, node=None))
    lines = m.partial_progress(pod, None, None, None)

    assert lines[0].startswith("T0 pod Pending") and "05:00:00Z" in lines[0]
    assert "n/a" in lines[1]
    assert m.partial_progress(None, None, None, None) == ["no vLLM pod was seen"]


def test_describe_phase():
    assert "Pending" in m.describe_phase(m.pod_info(make_pod(node=None)), None, None)
    assert "image pull" in m.describe_phase(m.pod_info(make_pod(node="n")), None, None)
    assert "readiness" in m.describe_phase(m.pod_info(make_pod(running=5)), None, None)


# ---- vLLM's own log lines ----------------------------------------------------------------------------------

LOG = "\n".join([
    "2026-11-11T05:03:20.000000000Z INFO 11-11 05:03:20 [api_server.py] vLLM API server version 0.33.0",
    "2026-11-11T05:04:10.500000000Z INFO 11-11 05:04:10 [model_loader.py] Loading weights took 41.20 seconds",
    "2026-11-11T05:04:30.000000000Z INFO 11-11 05:04:30 [core.py] init engine (profile, create kv cache, warmup model) took 18.5 seconds",
    "2026-11-11T05:04:31.000000000Z INFO:     Application startup complete.",
])


def test_log_timings_report_only_what_vllm_said():
    started = datetime(2026, 11, 11, 5, 3, 20, tzinfo=timezone.utc)

    found = m.log_timings(LOG, started)

    assert [text for _, text in found] == [
        "Loading weights took 41.20 seconds",
        "init engine (profile, create kv cache, warmup model) took 18.5 seconds",
    ]
    assert found[0][0] == 50.5


def test_no_matching_log_lines_means_nothing_is_invented():
    assert m.log_timings("INFO starting\nINFO ready", BASE) == []
    assert m.log_timings("", BASE) == []


# ---- inference check ------------------------------------------------------------------------------------------

def test_inference_success_and_failure_messages():
    assert m.classify_inference(200, {"response": "ready"}) is None
    assert m.classify_inference(200, {"response": ""}) == "inference failed: HTTP 200"
    assert m.classify_inference(502, {"error": "vllm_unavailable"}) == "inference failed: HTTP 502 (vllm_unavailable)"
    assert m.classify_inference(504, {"error": "request_deadline_exceeded", "prompt": "SECRET"}) == \
        "inference failed: HTTP 504 (request_deadline_exceeded)"
    assert m.classify_inference(500, None) == "inference failed: HTTP 500"


# ---- the report ---------------------------------------------------------------------------------------------------

def test_text_report_has_the_requested_sections_and_no_none():
    pod, claim, node, ms = scenario(inference=315)
    events = m.events_info([make_event("Pulling", 52), make_event("Pulled", 190, "pulled in 2m18s")])

    text = m.format_report(ms, pod, claim, node, events, [(50.5, "Loading weights took 41 seconds")],
                           BASE + timedelta(seconds=312), BASE + timedelta(seconds=315), [], {"nlb_healthy_at": BASE + timedelta(seconds=330)})

    for needle in ("vLLM Cold Start Measurement", "T0 Pod Pending", "T1 NodeClaim Created", "T2 Node Ready",
                   "T3 Container Started", "T4 vLLM Ready", "T5 First Inference", "Karpenter reaction",
                   "GPU capacity ready", "Total usable cold start", "Breakdown", "vLLM reported", "1 s resolution"):
        assert needle in text

    assert "None" not in text


def test_json_report_is_valid():
    pod, claim, node, ms = scenario(inference=315)
    events = m.events_info([])

    data = json.loads(m.as_json(ms, pod, claim, node, events, [], None, None, ["note"], {}))

    assert data["durations_seconds"]["Total usable cold start"] == 315
    assert data["timestamps"]["T0"] == "2026-11-11T05:00:00Z"
    assert data["nodeclaim"] == "gpu-xyz12"


# ---- port-forward robustness ------------------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, alive):
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True


def test_port_forward_restarts_when_kubectl_exited(monkeypatch):
    started = []
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or FakeProcess(alive=len(started) > 1))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda url, timeout=0: (_ for _ in ()).throw(OSError("refused")))

    forward = m.PortForward("default", "vllm-x")

    assert forward.healthy() is False       # first process was dead -> started a second one
    assert len(started) == 2
    assert started[0][-1] == "%d:8000" % forward.port
    forward.healthy()
    assert len(started) == 2                # the second one is alive: no further restart


def test_free_port_is_usable():
    assert 1024 < m.free_port() < 65536


# ---- the whole flow against a fake cluster -------------------------------------------------------------------------

class FakeKube:
    namespace = "default"

    def __init__(self, pods=(), events=(), claims=(), node=None, logs=""):
        self._pods, self._events, self._claims, self._node, self._logs = list(pods), list(events), list(claims), node, logs

    def pods(self):
        return self._pods

    def events(self, name):
        return self._events

    def nodeclaims(self):
        return self._claims

    def node(self, name):
        return self._node

    def plugin_pods(self, node_name):
        return []

    def logs(self, name):
        return self._logs


class FlowArgs:
    pod = None
    test_inference = False
    json = False
    interval = 0
    timeout = 0
    pod_timeout = 0
    nodeclaim_timeout = 0
    nlb_timeout = 1


def test_flow_on_an_already_ready_pod_prints_the_report(capsys):
    kube = FakeKube(
        pods=[make_pod(created=0, scheduled=50, running=200, ready=310, unschedulable=1)],
        events=[make_event("Nominated", 2, "Pod should schedule on: nodeclaim/gpu-xyz12")],
        claims=[make_claim()], node=make_node(), logs=LOG)
    printed = []

    assert m.measure(FlowArgs(), kube, out=printed.append) == 0

    text = printed[0]
    assert "Karpenter reaction         2s" in text
    assert "Node provisioning          38s" in text
    assert "GPU capacity ready         40s" in text
    assert "already Ready" in text


def test_flow_fails_clearly_when_no_vllm_pod_exists():
    with pytest.raises(m.MeasurementError, match="no vLLM pod") as excinfo:
        m.measure(FlowArgs(), FakeKube())

    assert excinfo.value.partial == ["no vLLM pod was seen"]


def test_flow_fails_with_progress_when_the_pod_never_gets_ready():
    kube = FakeKube(pods=[make_pod(created=0, node=None)], claims=[make_claim(node=None)],
                    events=[make_event("Nominated", 2, "nodeclaim/gpu-xyz12")])
    args = FlowArgs()
    args.nodeclaim_timeout = 999

    with pytest.raises(m.MeasurementError, match="not ready after 0s") as excinfo:
        m.measure(args, kube)

    assert excinfo.value.partial[0].startswith("T0 pod Pending")
    assert excinfo.value.partial[1].startswith("T1 NodeClaim created")


def test_flow_fails_fast_on_an_image_pull_failure():
    kube = FakeKube(pods=[make_pod(waiting="ImagePullBackOff", node="n")])

    with pytest.raises(m.MeasurementError, match="ImagePullBackOff"):
        m.measure(FlowArgs(), kube)


# ---- after-the-fact runs must not report late-start artefacts as cold-start numbers ----------------------------------

def test_posthoc_hides_first_request_and_total():
    _, _, _, ms = scenario(inference=1500)

    steps, summary = m.durations(ms, posthoc=True)

    assert dict(steps)["First request"] is None
    assert dict(summary)["Total usable cold start"] is None
    assert dict(steps)["Model initialization"] == 110          # Kubernetes-recorded numbers stay
    assert dict(summary)["GPU capacity ready"] == 40


def test_posthoc_without_inference_still_reports_pod_ready():
    _, _, _, ms = scenario()

    _, summary = m.durations(ms, posthoc=True)

    assert dict(summary)["Pod ready (no inference)"] == 310


def test_posthoc_report_prints_na_and_omits_the_nlb_wait():
    pod, claim, node, ms = scenario(inference=1500)
    events = m.events_info([])

    text = m.format_report(ms, pod, claim, node, events, [], BASE + timedelta(seconds=1489), BASE + timedelta(seconds=1500),
                           [], {"nlb_healthy_at": BASE + timedelta(seconds=1400), "posthoc": True})

    assert "First request              n/a" in text
    assert "Total usable cold start    n/a" in text
    assert "NLB target healthy" not in text
    assert "inference request latency" in text


def test_flow_on_a_ready_pod_with_inference_explains_why_totals_are_missing(monkeypatch):
    monkeypatch.setattr(m, "gateway_settings", lambda: ("https://api.example/invoke", "token"))
    monkeypatch.setattr(m, "nlb_target_healthy", lambda ip: True)
    monkeypatch.setattr(m, "send_inference", lambda url, token: (BASE + timedelta(seconds=1489), BASE + timedelta(seconds=1500)))
    kube = FakeKube(pods=[make_pod(created=0, scheduled=50, running=200, ready=310)],
                    events=[make_event("Nominated", 2, "nodeclaim/gpu-xyz12")], claims=[make_claim()], node=make_node())
    args = FlowArgs()
    args.test_inference = True
    printed = []

    assert m.measure(args, kube, out=printed.append) == 0

    assert "First request              n/a" in printed[0]
    assert "not reported" in printed[0]
    assert "inference request latency" in printed[0] and "11s" in printed[0]
    assert "token" not in printed[0].lower().replace("tokens", "")


# ---- T4b after the fact, from vLLM's own log ----------------------------------------------------------------------------

def test_app_started_comes_from_the_startup_log_line():
    assert m.log_app_started(LOG) == datetime(2026, 11, 11, 5, 4, 31, tzinfo=timezone.utc)
    assert m.log_app_started("INFO starting\nINFO loading") is None
    assert m.log_app_started("") is None


def test_flow_after_the_fact_uses_the_log_for_t4b_and_reports_the_probe_lag(capsys):
    kube = FakeKube(
        pods=[make_pod(created=0, scheduled=50, running=200, ready=310)],
        events=[make_event("Nominated", 2, "nodeclaim/gpu-xyz12")], claims=[make_claim()], node=make_node(),
        logs="2026-11-11T05:05:05.500000000Z INFO:     Application startup complete.\n")
    printed = []

    assert m.measure(FlowArgs(), kube, out=printed.append) == 0

    text = printed[0]
    assert "(pod Ready 2026-11-11T05:05:10Z, /health 2026-11-11T05:05:05.500Z; later one used)" in text
    assert "pod Ready after /health first passed" in text and "4.5s" in text
    assert "T4b is vLLM's 'Application startup complete' log time" in text


def test_a_live_health_observation_is_not_replaced_by_the_log(monkeypatch):
    class Forward:
        def __init__(self, ns, name):
            pass

        def healthy(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(m, "PortForward", Forward)
    ticks = iter([BASE + timedelta(seconds=n) for n in range(100, 200)])
    monkeypatch.setattr(m, "now_utc", lambda: next(ticks))
    kube = FakeKube(
        pods=[make_pod(created=0, scheduled=50, running=200)],
        events=[make_event("Nominated", 2, "nodeclaim/gpu-xyz12")], claims=[make_claim()], node=make_node(),
        logs="2026-11-11T05:05:05.500000000Z INFO:     Application startup complete.\n")
    seen = {"n": 0}
    args = FlowArgs()
    args.timeout = 60

    def pods_becoming_ready():
        seen["n"] += 1
        return [make_pod(created=0, scheduled=50, running=200, ready=310 if seen["n"] > 1 else None)]

    kube.pods = pods_becoming_ready
    printed = []

    assert m.measure(args, kube, out=printed.append) == 0

    assert "T4b is vLLM's" not in printed[0]
    assert "/health 2026-11-11T05:01:" in printed[0] or "/health 2026-11-11T05:02:" in printed[0]


def test_logs_are_read_from_the_start_not_the_tail(monkeypatch):
    calls = []
    monkeypatch.setattr(m.Kube, "run", lambda self, args, timeout=30: calls.append(args) or "")

    m.Kube("default").logs("vllm-x")

    assert "--limit-bytes" in calls[0] and "--tail" not in calls[0]
    assert "--timestamps" in calls[0]
