# beparasync/routes/debug.py
import os
import httpx
from fastapi import APIRouter
from sandbox_client import run_in_sandbox

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/sandbox-health")
async def sandbox_health():
    sandbox_url = os.getenv("SANDBOX_URL", "NOT_SET")

    try:
        r = httpx.get(sandbox_url + "/health", timeout=10)
        health = {"status": r.status_code, "body": r.text}
    except Exception as e:
        health = {"error": str(e)}

    result = await run_in_sandbox(
        code='import os, pathlib\nprint("cwd:", os.getcwd())\npathlib.Path("apps/test-debug").mkdir(parents=True, exist_ok=True)\npathlib.Path("apps/test-debug/index.html").write_text("<h1>debug</h1>")\nprint("done")',
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": "debug"},
        packages=[],
        agent_id="debug",
        timeout=20,
    )

    return {
        "sandbox_url": sandbox_url,
        "health": health,
        "execution": {
            "exit_code": result.get("exit_code"),
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
            "output_files": list(result.get("output_files", {}).keys()),
        }
    }


@router.get("/sandbox-snapshot-test")
async def sandbox_snapshot_test():
    """
    Three runs to isolate exactly where snapshot fails:
    Run A — unique filename every time (timestamp-based) → must appear in output_files
    Run B — write to /workspace/ absolute path → must NOT appear (different dir)
    Run C — write nested relative path with unique name → must appear
    """
    import time
    ts = int(time.time())

    # ── Run A: unique relative file, no subdirectory ──────────────────────
    result_a = await run_in_sandbox(
        code=f'open("unique_{ts}.txt","w").write("hello")\nprint("wrote unique_{ts}.txt")',
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": "debug"},
        packages=[],
        agent_id="debug",
        timeout=15,
    )

    # ── Run B: absolute /workspace/ path ──────────────────────────────────
    result_b = await run_in_sandbox(
        code=f'import pathlib\np=pathlib.Path("/workspace/apps/abs-test")\np.mkdir(parents=True,exist_ok=True)\n(p/"index_{ts}.html").write_text("<h1>abs</h1>")\nprint("wrote to /workspace/")',
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": "debug"},
        packages=[],
        agent_id="debug",
        timeout=15,
    )

    # ── Run C: unique nested relative path ────────────────────────────────
    result_c = await run_in_sandbox(
        code=f'import pathlib\np=pathlib.Path("apps/rel-test-{ts}")\np.mkdir(parents=True,exist_ok=True)\n(p/"index.html").write_text("<h1>rel</h1>")\nprint("wrote to apps/rel-test-{ts}/")',
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": "debug"},
        packages=[],
        agent_id="debug",
        timeout=15,
    )

    return {
        "timestamp": ts,
        "run_A_unique_flat": {
            "exit_code": result_a.get("exit_code"),
            "stdout": result_a.get("stdout", "").strip(),
            "output_files": list(result_a.get("output_files", {}).keys()),
            "verdict": "PASS" if any(f"unique_{ts}" in f for f in result_a.get("output_files", {})) else "FAIL",
        },
        "run_B_absolute_workspace": {
            "exit_code": result_b.get("exit_code"),
            "stdout": result_b.get("stdout", "").strip(),
            "output_files": list(result_b.get("output_files", {}).keys()),
            "verdict": "CONFIRMED_BUG" if not result_b.get("output_files") else "NOT_A_BUG",
        },
        "run_C_nested_relative": {
            "exit_code": result_c.get("exit_code"),
            "stdout": result_c.get("stdout", "").strip(),
            "output_files": list(result_c.get("output_files", {}).keys()),
            "verdict": "PASS" if any(f"rel-test-{ts}" in f for f in result_c.get("output_files", {})) else "FAIL",
        },
    }