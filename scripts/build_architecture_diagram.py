#!/usr/bin/env python3
"""Build docs/diagrams/architecture.svg, the README architecture diagram.

The layout is hand-placed (fixed coordinates) so it reads like a conventional AWS
architecture diagram: AWS Cloud > Region > VPC > subnet boundaries, service icons,
numbered request steps and a legend. Icons are inlined so the SVG has no external
references and renders as an <img> on GitHub.

Icons: the AWS Architecture Icons as packaged by the npm package aws-icons@3.3.0.
Fetch them without installing anything:

    mkdir -p /tmp/aws-icons && cd /tmp/aws-icons
    npm pack aws-icons@3.3.0 --ignore-scripts && tar -xzf aws-icons-3.3.0.tgz

Then, from the repository root:

    python3 scripts/build_architecture_diagram.py /tmp/aws-icons/package/icons

PNG export (optional), with any Chromium-based browser:

    chrome --headless=new --screenshot=docs/diagrams/architecture.png \
      --window-size=1780,1190 --force-device-scale-factor=2 \
      file://$PWD/docs/diagrams/architecture.svg

Animated walkthrough (docs/diagrams/architecture-animated.svg): the same drawing,
one workflow at a time (see WORKFLOWS below), looping. It is a single vector SVG
animated with SMIL, so it stays sharp at any size and plays inside a README <img>:

    python3 scripts/build_architecture_diagram.py /tmp/aws-icons/package/icons --animated

Facts shown here must match the code and Terraform; see docs/architecture.md.
"""

import re
import sys
from pathlib import Path

W, H = 1780, 1190
FONT = "'Amazon Ember', 'Helvetica Neue', Helvetica, Arial, sans-serif"

# Colours: AWS group colours for boundaries, one colour per kind of flow.
INK, MUTED = "#232F3E", "#5F6B7A"
BLUE, AMBER, GREEN, GREY = "#2563EB", "#C2410C", "#2E7D32", "#64748B"

ICONS = {
    "cognito": "architecture-service/AmazonCognito",
    "apigw": "architecture-service/AmazonAPIGateway",
    "lambda": "architecture-service/AWSLambda",
    "bedrock": "architecture-service/AmazonBedrock",
    "agentcore": "architecture-service/AmazonBedrockAgentCore",
    "sfn": "architecture-service/AWSStepFunctions",
    "sqs": "architecture-service/AmazonSimpleQueueService",
    "cloudwatch": "architecture-service/AmazonCloudWatch",
    "eks": "architecture-service/AmazonElasticKubernetesService",
    "ec2": "architecture-service/AmazonEC2",
    "nlb": "resource/ElasticLoadBalancingNetworkLoadBalancer",
    "s3vectors": "resource/AmazonSimpleStorageServiceS3Vectors",
    "client": "resource/Client",
    "app": "resource/GenericApplication",
    "sdk": "resource/SDK",
    "elb": "architecture-service/ElasticLoadBalancing",
    "router": "resource/AmazonVPCRouter",
    "user": "resource/User",
    "server": "resource/Server",
    "ec2inst": "resource/AmazonEC2Instance",
    "g_cloud": "architecture-group/AWSCloudlogo",
    "g_region": "architecture-group/Region",
    "g_vpc": "architecture-group/VirtualprivatecloudVPC",
    "g_private": "architecture-group/Privatesubnet",
    "g_dc": "architecture-group/Corporatedatacenter",
    "g_ec2": "architecture-group/EC2instancecontents",
}

out: list[str] = []


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def symbols(icon_dir: Path) -> str:
    """Turn each icon file into a <symbol>, prefixing internal ids to keep them unique."""
    parts = []
    for key, rel in ICONS.items():
        src = (icon_dir / f"{rel}.svg").read_text()
        view_box = re.search(r'viewBox="([^"]+)"', src).group(1)
        body = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", src, flags=re.S)
        body = re.sub(r"<title>.*?</title>", "", body, flags=re.S)
        body = re.sub(r'id="([^"]+)"', rf'id="{key}-\1"', body)
        body = re.sub(r"url\(#([^)]+)\)", rf"url(#{key}-\1)", body)
        body = re.sub(r'href="#([^"]+)"', rf'href="#{key}-\1"', body)
        parts.append(f'<symbol id="i-{key}" viewBox="{view_box}">{body}</symbol>')
    return "\n".join(parts)


def text(x, y, s, size=13, weight="normal", fill=INK, anchor="middle", style=""):
    out.append(
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}"{f" font-style={chr(34)}{style}{chr(34)}" if style else ""}>'
        f"{esc(s)}</text>"
    )


def icon(key, cx, cy, size=52):
    out.append(f'<use href="#i-{key}" x="{cx - size / 2}" y="{cy - size / 2}" width="{size}" height="{size}"/>')


def node(key, cx, cy, title, lines=(), size=52, note=None):
    """A service icon with a bold title and up to two detail lines underneath."""
    icon(key, cx, cy, size)
    y = cy + size / 2 + 17
    text(cx, y, title, 13.5, "bold")
    for line in lines:
        y += 16
        text(cx, y, line, 12, fill=MUTED)
    if note:
        y += 16
        text(cx, y, note, 12, "bold", AMBER, style="italic")


def group(x, y, w, h, label, stroke, icon_key=None, fill="none", dash=None, label_fill=None, width=1.4):
    """An AWS-style boundary: rectangle, icon in the top-left corner, label beside it."""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"{d}/>')
    tx = x + 10
    if icon_key:
        out.append(f'<use href="#i-{icon_key}" x="{x}" y="{y}" width="28" height="28"/>')
        tx = x + 36
    text(tx, y + 19, label, 13, "bold", label_fill or stroke, anchor="start")


# Animated SVG: the base layer is the whole diagram with every flow dimmed; per-scene
# overlays (halos behind the icons, flows and captions on top) are animated with SMIL.
ANIMATED = None  # None for the static diagram, else {"halos": str, "overlay": str}
DIM = 0.13
LENGTHS: dict[str, float] = {}  # edge id -> length, filled by the static build
POINTS: dict[str, list] = {}  # edge id -> polyline
STYLE: dict[str, tuple] = {}  # edge id -> (color, width, dash, arrow)
PILLS: dict[str, list] = {}  # edge id -> pill arguments


def polyline(points):
    return "M " + " L ".join(f"{px},{py}" for px, py in points)


def path(points, color, width=1.6, dash=None, arrow=True, eid=None):
    if eid and ANIMATED is None:
        POINTS[eid] = points
        STYLE[eid] = (color, width, dash, arrow)
        LENGTHS[eid] = sum(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 for (ax, ay), (bx, by) in zip(points, points[1:]))
    da = f' stroke-dasharray="{dash}"' if dash else ""
    marker = f' marker-end="url(#arrow-{color[1:]})"' if arrow else ""
    op = f' opacity="{DIM}"' if ANIMATED is not None and eid else ""
    out.append(
        f'<path d="{polyline(points)}" fill="none" stroke="{color}" stroke-width="{width}"{da} '
        f'stroke-linejoin="round"{marker}{op}/>'
    )


def pill(cx, cy, s, color, step=None, eid=None):
    """An edge label on a white background, optionally with a numbered step badge."""
    if eid and ANIMATED is None:
        PILLS.setdefault(eid, []).append((cx, cy, s, color, step))
    w = len(s) * 6.7 + 16 + (22 if step else 0)
    x = cx - w / 2
    dim = ANIMATED is not None and eid
    if dim:
        out.append(f'<g opacity="{DIM + 0.1}">')
    out.append(f'<rect x="{x}" y="{cy - 11}" width="{w}" height="22" rx="11" fill="#fff" stroke="{color}" stroke-width="1"/>')
    tx = x + 10
    if step:
        out.append(f'<circle cx="{x + 12}" cy="{cy}" r="8.5" fill="{color}"/>')
        text(x + 12, cy + 4, str(step), 11, "bold", "#fff")
        tx = x + 26
    text(tx, cy + 4, s, 11.5, "bold", color, anchor="start")
    if dim:
        out.append("</g>")


def build(icon_dir: Path) -> str:
    out.clear()

    # ---------- Title ----------
    text(24, 38, "Enterprise AI Platform on AWS · architecture (dev environment)", 22, "bold", anchor="start")
    text(W - 24, 38, "The caller selects one backend per request · no automatic routing or fallback",
         13, fill=MUTED, anchor="end")

    # ---------- Boundaries ----------
    group(16, 380, 226, 420, "Callers (outside AWS)", "#7D8998", dash="5 4", label_fill=MUTED)
    group(30, 574, 198, 214, "Inference Playground · local", "#9CA3AF", fill="#F9FAFB", label_fill=MUTED, width=1)
    group(256, 66, 1184, 978, "AWS Cloud", INK, "g_cloud")
    group(276, 106, 1144, 918, "AWS Region", "#00A4A6", "g_region", dash="6 4")
    group(470, 436, 760, 570, "VPC (default VPC)", "#8C4FFF", "g_vpc")
    group(496, 478, 262, 200, "Private subnet", "#00A4A6", "g_private", fill="#E6F6F7", label_fill="#007D7F")
    group(770, 478, 440, 336, "Amazon EKS cluster", "#ED7100", "eks")
    group(930, 516, 262, 150, "GPU node · g5.xlarge", "#ED7100", "g_ec2", fill="#FFF7EF")
    group(820, 140, 580, 270, "Supporting integrations · optional, per request", GREY, dash="5 4", label_fill=MUTED)
    group(1470, 790, 288, 214, "On-premises (outside this repo)", "#7D8998", "g_dc", dash="6 4", label_fill=MUTED)

    if ANIMATED is not None:
        out.append(ANIMATED["halos"])  # behind the icons

    # ---------- Callers (outside AWS) ----------
    node("app", 128, 448, "API client", ["machine-to-machine", "client credentials"], 48)
    node("client", 78, 636, "Browser", ["127.0.0.1"], 40)
    node("sdk", 180, 636, "Local BFF", ["token cache"], 40)
    for i, line in enumerate(["forwards the request fields", "secret stays in the BFF", "cannot approve writes"]):
        text(129, 728 + i * 17, line, 11.5, fill=MUTED)
    node("user", 1612, 215, "Operator", ["scripts/approve.py"], 48)

    # ---------- Entry ----------
    node("cognito", 380, 250, "Amazon Cognito", ["client-credentials token"])
    node("apigw", 380, 560, "API Gateway", ["HTTP API · JWT authorizer", "throttling"])
    node("elb", 520, 300, "NLB discovery", ["Tagging + ELB APIs", "first request, then cached"], 44)

    # ---------- Gateway ----------
    node("lambda", 660, 560, "Gateway Lambda", ["agent loop · model adapter", "MCP client"])

    # ---------- Inference backends ----------
    node("bedrock", 742, 215, "Amazon Bedrock", ["Converse API", "Nova Micro / Nova Lite"])
    node("nlb", 840, 580, "Internal NLB", ["Lambda SG only", "found by tag"], 48)

    # vLLM pod inside the GPU node
    out.append('<rect x="962" y="560" width="200" height="72" rx="8" fill="#fff" stroke="#ED7100" stroke-width="1.2"/>')
    text(1062, 586, "vLLM pod · 1 replica", 13.5, "bold")
    text(1062, 604, "Qwen2.5-7B-Instruct", 12, fill=MUTED)
    text(1062, 620, "node via Karpenter, from zero", 12, fill=MUTED)

    # Cluster add-ons
    out.append('<rect x="790" y="700" width="402" height="96" rx="6" fill="#fff" stroke="#F5B98A" stroke-width="1"/>')
    icon("ec2inst", 822, 748, 40)
    text(852, 724, "Cluster add-ons", 13, "bold", anchor="start")
    text(852, 742, "Karpenter (GPU NodePool) · AWS Load Balancer", 12, fill=MUTED, anchor="start")
    text(852, 758, "Controller · NVIDIA device plugin", 12, fill=MUTED, anchor="start")
    text(852, 774, "CloudWatch Observability add-on", 12, fill=MUTED, anchor="start")

    node("ec2", 660, 900, "EC2 router", ["NAT for AWS APIs · WireGuard", "configured outside Terraform"])
    node("server", 1612, 880, "GPU host · vLLM", ["Nginx in front of replicas", "(operator-described)"], 52)

    # ---------- Supporting integrations ----------
    node("s3vectors", 880, 215, "Knowledge Base", ["S3 Vectors · RAG"], 48)
    node("agentcore", 1010, 215, "AgentCore Runtime", ["MCP · 3 read-only tools"])
    node("sfn", 1172, 215, "Step Functions", ["approval workflow"])
    node("sqs", 1322, 215, "SQS queue", ["task token"])
    node("lambda", 1322, 330, "write-executor", ["applies approved change"], 40)

    # ---------- Observability ----------
    node("cloudwatch", 1325, 600, "Amazon CloudWatch", ["logs · EMF metrics", "EKS · vLLM · GPU metrics", "dashboard"])

    # ---------- Flows ----------
    # Entry: every request (grey token, blue invoke path)
    path([(106, 636), (156, 636)], GREY, 1.6, eid="bff")
    path([(242, 430), (300, 430), (300, 250), (352, 250)], GREY, eid="token")
    pill(300, 340, "token", GREY, 1, eid="token")
    path([(242, 562), (350, 562)], BLUE, 3, eid="api")
    pill(294, 562, "POST /invoke", BLUE, 2, eid="api")
    path([(408, 562), (630, 562)], BLUE, 3, eid="invoke")
    pill(520, 562, "invoke", BLUE, 3, eid="invoke")

    # Inference: exactly one per request, named by the caller
    path([(656, 532), (656, 215), (712, 215)], BLUE, 3, eid="bedrock")
    pill(656, 385, "provider=bedrock", BLUE, 4, eid="bedrock")
    path([(640, 532), (640, 300), (546, 300)], GREY, 1.6, eid="discover")
    pill(594, 300, "find NLB", GREY, eid="discover")
    path([(690, 580), (812, 580)], BLUE, 3, eid="eks")
    pill(752, 580, "vllm · eks", BLUE, 4, eid="eks")
    path([(866, 590), (958, 590)], BLUE, 3, eid="eks2")
    path([(660, 660), (660, 868)], AMBER, 2.6, "8 5", eid="onprem")
    pill(660, 724, "vllm · onprem", AMBER, 4, eid="onprem")
    path([(690, 900), (1582, 900)], AMBER, 2.6, "8 5", eid="wireguard")
    pill(1000, 900, "WireGuard tunnel", AMBER, eid="wireguard")
    icon("router", 660, 810, 30)
    text(640, 814, "subnet route table", 11.5, fill=AMBER, anchor="end")

    # Supporting integrations (one bus from the Lambda)
    path([(676, 532), (676, 422), (880, 422)], GREEN, 1.8, arrow=False, eid="bus1")
    path([(880, 422), (1010, 422)], GREEN, 1.8, arrow=False, eid="bus2")
    path([(1010, 422), (1100, 422)], GREEN, 1.8, arrow=False, eid="bus3")
    path([(880, 422), (880, 290)], GREEN, 1.8, eid="rag")
    pill(880, 372, "rag=true", GREEN, eid="rag")
    path([(1010, 422), (1010, 290)], GREEN, 1.8, eid="mcp")
    pill(1010, 372, "MCP session · tools", GREEN, eid="mcp")
    path([(1100, 422), (1100, 205), (1142, 205)], GREEN, 1.8, eid="write")
    pill(1100, 330, "write → 202", GREEN, eid="write")
    path([(1200, 205), (1294, 205)], GREEN, 1.6, eid="sfn_sqs")
    path([(1352, 205), (1586, 205)], GREEN, 1.6, eid="sqs_op")
    pill(1470, 205, "read request", GREEN, eid="sqs_op")
    path([(1612, 188), (1612, 124), (1172, 124), (1172, 186)], GREEN, 1.6, eid="op_sfn")
    pill(1470, 124, "approve / reject (task token)", GREEN, eid="op_sfn")
    path([(1172, 290), (1172, 330), (1298, 330)], GREEN, 1.6, eid="exec")
    pill(1228, 330, "approved", GREEN, eid="exec")

    # Telemetry
    path([(745, 678), (745, 836), (1325, 836), (1325, 702)], GREY, 1.4, "2 4", eid="lambda_cw")
    pill(990, 836, "Lambda logs · EMF metrics", GREY, eid="lambda_cw")
    path([(1210, 600), (1295, 600)], GREY, 1.4, "2 4", eid="telemetry")

    if ANIMATED is not None:
        out.append(ANIMATED["overlay"])  # flows, dots and captions on top
        return finish(icon_dir)
    legend()
    return finish(icon_dir)


LY = 1074  # legend / caption panel baseline


def caption(index, title, lines):
    """The panel at the bottom: scene number, title and two lines of explanation."""
    ly = LY
    out.append(f'<rect x="24" y="{ly - 22}" width="{W - 48}" height="116" rx="8" fill="#F8FAFC" stroke="#E2E8F0"/>')
    out.append(f'<rect x="44" y="{ly - 6}" width="72" height="28" rx="14" fill="{INK}"/>')
    text(80, ly + 13, index, 13.5, "bold", "#fff")
    text(130, ly + 14, title, 19, "bold", anchor="start")
    for i, line in enumerate(lines):
        text(44, ly + 44 + i * 20, line, 14, fill=MUTED, anchor="start")


def legend():
    ly = LY
    out.append(f'<rect x="24" y="{ly - 22}" width="{W - 48}" height="116" rx="8" fill="#F8FAFC" stroke="#E2E8F0"/>')
    text(44, ly, "Legend", 13, "bold", anchor="start")
    items = [
        (BLUE, 3, None, "Request and inference path · the caller names one backend per request"),
        (AMBER, 2.6, "8 5", "On-prem path · implemented, no successful end-to-end request yet"),
        (GREEN, 1.8, None, "Supporting integrations · RAG on request; tools and writes when the model asks"),
        (GREY, 1.4, "2 4", "Authentication token · NLB discovery · telemetry"),
    ]
    for i, (c, w, dash, label) in enumerate(items):
        x = 44 + (i % 2) * 560
        y = ly + 22 + (i // 2) * 22
        path([(x, y), (x + 44, y)], c, w, dash)
        text(x + 56, y + 4, label, 12, anchor="start")
    text(1160, ly, "Notes", 13, "bold", anchor="start")
    notes = [
        "Steps: 1 token · 2 call the API · 3 invoke · 4 selected backend.",
        "Every request opens an MCP session to AgentCore.",
        "Lambda egress to AWS APIs goes through the EC2 router's NAT.",
        "No routing, retries or failover between backends.",
        "Icons: AWS Architecture Icons.",
    ]
    for i, n in enumerate(notes):
        text(1160, ly + 19 + i * 17, n, 11.5, fill=MUTED, anchor="start")


def finish(icon_dir: Path) -> str:
    markers = "".join(
        f'<marker id="arrow-{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
        for c in (BLUE, AMBER, GREEN, GREY)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'font-family="{FONT}" role="img" aria-labelledby="t">'
        f'<title id="t">Enterprise AI Platform on AWS: architecture</title>'
        f"<defs>{markers}{symbols(icon_dir)}</defs>"
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>' + "\n".join(out) + "</svg>\n"
    )


# ---------- Animated walkthrough ----------
TEAL = "#0D9488"  # response dots
NODES = {
    "client": (128, 448), "browser": (78, 636), "bff": (180, 636), "cognito": (380, 250),
    "apigw": (380, 560), "discovery": (520, 300), "lambda": (660, 560), "bedrock": (742, 215),
    "nlb": (840, 580), "vllm": (1062, 596), "router": (660, 900), "onprem": (1612, 880),
    "kb": (880, 215), "agentcore": (1010, 215), "sfn": (1172, 215), "sqs": (1322, 215),
    "operator": (1612, 215), "exec": (1322, 330), "cloudwatch": (1325, 600),
}


def back(*eids):
    """A response route: the given edges walked backwards, in the order given."""
    pts = []
    for eid in eids:
        pts += list(reversed(POINTS[eid]))
    return pts


# Response routes (teal dot, no line). Built lazily because POINTS fills during the static build.
RETURNS = {
    "to_client": lambda: back("invoke", "api") + [(200, 448), (156, 448)],
    "to_browser": lambda: back("invoke", "api") + [(204, 636)] + back("bff"),
    "from_bedrock": lambda: back("bedrock"),
    "from_kb": lambda: back("rag", "bus1"),
    "from_mcp": lambda: back("mcp", "bus2", "bus1"),
    "from_eks": lambda: back("eks2", "eks"),
    "from_onprem": lambda: back("wireguard", "onprem"),
    "from_discovery": lambda: back("discover"),
}

# Each workflow: title, caption lines, steps, highlighted nodes.
# A step is an edge id (drawn forwards) or "ret:<route>" (a response dot travelling back).
WORKFLOWS = [
    ("Entry · every request",
     ["The client gets a JWT from Amazon Cognito with its client credentials and calls POST /invoke on API Gateway.",
      "API Gateway checks the token and invokes the gateway Lambda, which opens an MCP session to AgentCore."],
     ["token", "api", "invoke", "bus1", "bus2", "mcp", "ret:from_mcp"],
     ["client", "cognito", "apigw", "lambda", "agentcore"]),
    ("Bedrock inference · provider=bedrock (the default)",
     ["The Lambda calls Amazon Bedrock Converse through the EC2 router's NAT:",
      "Nova Micro for model_class=fast, Nova Lite for quality. The answer goes back to the client."],
     ["api", "invoke", "bedrock", "ret:from_bedrock", "ret:to_client"],
     ["client", "apigw", "lambda", "bedrock"]),
    ("vLLM on EKS · provider=vllm, target=eks",
     ["On the first request the Lambda finds the internal NLB by its Kubernetes tag and caches the address,",
      "then calls vLLM (Qwen2.5-7B) on the Karpenter GPU node through the NLB."],
     ["api", "invoke", "discover", "ret:from_discovery", "eks", "eks2", "ret:from_eks", "ret:to_client"],
     ["client", "apigw", "discovery", "lambda", "nlb", "vllm"]),
    ("vLLM on-premises · provider=vllm, target=onprem",
     ["The Lambda subnet's route table sends the request to the EC2 router, which forwards it",
      "through the WireGuard tunnel to vLLM on the on-premises GPU host. No fallback to EKS or Bedrock."],
     ["api", "invoke", "onprem", "wireguard", "ret:from_onprem", "ret:to_client"],
     ["client", "apigw", "lambda", "router", "onprem"]),
    ("RAG · rag=true (works with any provider)",
     ["Before the model call, the Lambda retrieves chunks from the Bedrock Knowledge Base (S3 Vectors)",
      "and adds them to the prompt; then the selected backend answers (Bedrock shown)."],
     ["api", "invoke", "bus1", "rag", "ret:from_kb", "bedrock", "ret:from_bedrock", "ret:to_client"],
     ["client", "apigw", "lambda", "kb", "bedrock"]),
    ("MCP tool calling (the model decides)",
     ["When the model asks for a tool, the Lambda calls the read-only MCP server on AgentCore",
      "and gives the result back to the model, which then answers. The loop runs up to 5 steps."],
     ["api", "invoke", "bedrock", "ret:from_bedrock", "bus1", "bus2", "mcp", "ret:from_mcp", "bedrock", "ret:from_bedrock",
      "ret:to_client"],
     ["client", "apigw", "lambda", "bedrock", "agentcore"]),
    ("Human-approved writes (the model decides)",
     ["A proposed write starts Step Functions and the API returns 202. The proposal waits on SQS until an operator",
      "approves or rejects it with scripts/approve.py; only an approved change runs in the write-executor Lambda."],
     ["api", "invoke", "bus1", "bus2", "bus3", "write", "ret:to_client", "sfn_sqs", "sqs_op", "op_sfn", "exec"],
     ["client", "apigw", "lambda", "sfn", "sqs", "operator", "exec"]),
    ("Inference Playground (local UI)",
     ["The browser calls the local BFF on 127.0.0.1. The BFF gets and caches the Cognito token, keeps the secret,",
      "and calls API Gateway like any client. The page shows the answer; it cannot approve writes."],
     ["bff", "token", "api", "invoke", "ret:to_browser"],
     ["browser", "bff", "cognito", "apigw", "lambda"]),
]

# Timing (seconds): pause before a scene's first step, per 40 px of line drawn, per 55 px of a
# response dot, hold after the scene, final overview, cross-fade between scenes.
LEAD, DRAW_PER_40PX, BACK_PER_55PX, HOLD, FINAL, FADE = 0.6, 0.035, 0.035, 2.6, 3.5, 0.25


def capture(fn, *args):
    """Run a drawing helper and return what it drew, leaving the main drawing untouched."""
    saved = out[:]
    out.clear()
    fn(*args)
    drawn = "".join(out)
    out[:] = saved
    return drawn


def length(pts):
    return sum(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 for (ax, ay), (bx, by) in zip(pts, pts[1:]))


def animated(icon_dir: Path) -> str:
    """One SVG that plays every workflow in turn, looping. SMIL, so it animates inside <img>."""
    global ANIMATED
    ANIMATED = None
    build(icon_dir)  # fills POINTS, STYLE, PILLS and LENGTHS

    # 1. Timeline: when each scene runs and when each of its steps runs.
    scenes, t = [], 0.0
    for title, lines, steps, halo in WORKFLOWS:
        start, t, timed = t, t + LEAD, []
        for step in steps:
            if step.startswith("ret:"):
                pts = RETURNS[step[4:]]()
                dur = max(4, round(length(pts) / 55)) * BACK_PER_55PX
            else:
                pts = POINTS[step]
                dur = max(4, round(LENGTHS[step] / 40)) * DRAW_PER_40PX
            timed.append((step, pts, t, t + dur))
            t += dur
        t += HOLD
        scenes.append((start, t, title, lines, timed, halo))
    final_start, total = t, t + FINAL

    def anim(attr, pts, tag="animate", extra=""):
        """pts: [(seconds, value)]; one shared, looping clock for every animation."""
        pts = sorted(pts)
        if pts[0][0] > 0:
            pts.insert(0, (0.0, pts[0][1]))
        if pts[-1][0] < total:
            pts.append((total, pts[-1][1]))
        keys, last = [], -1.0
        for sec, _ in pts:
            k = max(sec / total, last + 0.0001)
            keys.append(min(k, 1.0))
            last = k
        keys[-1] = 1.0
        kt = ";".join(f"{k:.5f}" for k in keys)
        vals = ";".join(str(v) for _, v in pts)
        name = "keyPoints" if tag == "animateMotion" else "values"
        attr_part = "" if tag == "animateMotion" else f'attributeName="{attr}" '
        return (f'<{tag} {attr_part}dur="{total:.3f}s" repeatCount="indefinite" calcMode="linear" '
                f'keyTimes="{kt}" {name}="{vals}"{extra}/>')

    def window(start, end):
        return anim("opacity", [(start, 0), (start + FADE, 1), (end - FADE, 1), (end, 0)])

    def dot(pts, a, b, color, radius=7):
        motion = anim(None, [(a, 0), (b, 1)], "animateMotion", f' path="{polyline(pts)}"')
        show = anim("opacity", [(a - 0.01, 0), (a, 1), (b, 1), (b + 0.05, 0)])
        return (f'<g opacity="0">{motion}{show}'
                f'<circle r="{radius + 5}" fill="{color}" opacity="0.25"/>'
                f'<circle r="{radius}" fill="{color}" stroke="#fff" stroke-width="2"/></g>')

    def full_edge(eid):
        color, width, dash, arrow = STYLE[eid]
        return capture(path, POINTS[eid], color, width, dash, arrow)

    def pills(eid):
        return "".join(capture(pill, *args) for args in PILLS.get(eid, []))

    halos, overlay, masks = [], [], []
    for n, (start, end, title, lines, timed, halo) in enumerate(scenes, 1):
        # Halos (drawn behind the icons).
        h = []
        for name in halo:
            if name == "vllm":  # the pod is a box, not an icon
                h.append('<rect x="948" y="546" width="228" height="100" rx="14" fill="#FACC15" opacity="0.35"/>')
            else:
                hx, hy = NODES[name]
                h.append(f'<circle cx="{hx}" cy="{hy}" r="40" fill="#FACC15" opacity="0.3"/>')
        halos.append(f'<g opacity="0">{window(start, end)}{"".join(h)}</g>')

        # Flows, dots and the caption (drawn on top).
        g, drawn = [], set()
        for step, pts, a, b in timed:
            if step.startswith("ret:"):
                g.append(dot(pts, a, b, TEAL))
                continue
            color = STYLE[step][0]
            if step not in drawn:
                drawn.add(step)
                mid = f"m{n}-{step}"
                masks.append(
                    f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}">'
                    f'<path d="{polyline(pts)}" fill="none" stroke="#fff" stroke-width="24" stroke-linecap="square" '
                    f'pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1">'
                    f'{anim("stroke-dashoffset", [(a, 1), (b, 0)])}</path></mask>')
                g.append(f'<g mask="url(#{mid})">{full_edge(step)}</g>')
                g.append(f'<g opacity="0">{anim("opacity", [(a, 0), (a + 0.15, 1)])}{pills(step)}</g>')
            g.append(dot(pts, a, b, color, 6))
        g.append(capture(caption, f"{n} / {len(scenes)}", title, lines))
        overlay.append(f'<g opacity="0">{window(start, end)}{"".join(g)}</g>')

    # Final overview: every flow at full strength, with the legend.
    every = "".join(full_edge(eid) + pills(eid) for eid in POINTS)
    overlay.append(f'<g opacity="0">{window(final_start, total + FADE)}{every}{capture(legend)}</g>')

    ANIMATED = {"halos": "".join(halos), "overlay": f'<defs>{"".join(masks)}</defs>' + "".join(overlay)}
    try:
        svg = build(icon_dir)
    finally:
        ANIMATED = None
    print(f"animation: {len(scenes)} scenes, {total:.1f} s loop")
    return svg.replace("Enterprise AI Platform on AWS: architecture</title>",
                       "Enterprise AI Platform on AWS: request flows (animated)</title>", 1)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent.parent / "docs" / "diagrams"  # output next to the README images
    if len(sys.argv) == 3 and sys.argv[2] == "--animated":
        (here / "architecture-animated.svg").write_text(animated(Path(sys.argv[1])))
        print(f"wrote {here / 'architecture-animated.svg'}")
    elif len(sys.argv) == 2:
        (here / "architecture.svg").write_text(build(Path(sys.argv[1])))
        print(f"wrote {here / 'architecture.svg'}")
    else:
        sys.exit(__doc__)
