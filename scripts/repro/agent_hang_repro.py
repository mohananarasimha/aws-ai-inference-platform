"""
Run the real agent loop against a local MCP server and a vLLM socket that
never answers, with the 24 s request budget.

    python3 scripts/repro/local_mcp_server.py &     # first
    python3 scripts/repro/agent_hang_repro.py

Local only. Expected (and observed on 2026-11-11): the run ends after about
20 s (the vLLM call limit) with a controlled exception, and nothing hangs
afterwards. The production outage ended at Lambda's 30 s hard timeout
instead, so this local path does NOT reproduce the gap; see
docs/known-limitations.md.
"""
import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway" / "lambda"))
import agent  # noqa: E402
import mcp_client  # noqa: E402
from deadline import RequestDeadline  # noqa: E402

mcp_client.build_agentcore_mcp_url = lambda: "http://127.0.0.1:18765/mcp"

hung = socket.socket()
hung.bind(("127.0.0.1", 0))
hung.listen(5)
held = []
threading.Thread(target=lambda: [held.append(hung.accept()) for _ in range(9)], daemon=True).start()
os.environ["VLLM_BASE_URL"] = f"http://127.0.0.1:{hung.getsockname()[1]}"

start = time.time()
deadline = RequestDeadline(24)
try:
    asyncio.run(agent.run_agent("hi", "Qwen/x", "Bearer x", "repro", provider="vllm", deadline=deadline))
except BaseException as exc:
    print("exit after", round(time.time() - start, 1), "s:", type(exc).__name__)
