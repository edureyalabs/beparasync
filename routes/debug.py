# beparasync/routes/debug.py
from fastapi import APIRouter
import asyncio
from sandbox_client import run_in_sandbox

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/sandbox-health")
async def sandbox_health():
    import os
    sandbox_url = os.getenv("SANDBOX_URL", "NOT SET")
    
    # Test 1: raw health
    import httpx
    try:
        r = httpx.get(sandbox_url + "/health", timeout=10)
        health = {"status": r.status_code, "body": r.text}
    except Exception as e:
        health = {"error": str(e)}

    # Test 2: actual code execution
    result = await run_in_sandbox(
        code='import os\nprint("cwd:", os.getcwd())\nopen("test.txt","w").write("hello")\nprint("done")',
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": "test"},
        packages=[],
        agent_id="test",
        timeout=15,
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