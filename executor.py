# beparasync/executor.py
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import traceback
from typing import Any

from db import supabase


PY_TYPE_MAP = {
    "string": "str", "number": "float", "integer": "int",
    "boolean": "bool", "array": "list", "object": "dict",
}


def build_full_code(tool_name: str, parameters: dict, body: str) -> str:
    props = parameters.get("properties", {})
    args  = [
        f"{name}: {PY_TYPE_MAP.get(meta.get('type', 'string'), 'str')}"
        for name, meta in props.items()
    ]
    signature = f"def {tool_name}({', '.join(args)}):"
    indented  = textwrap.indent(body.strip(), "    ")
    return f"{signature}\n{indented}"


async def fetch_secrets(toolset_id: str) -> dict[str, str]:
    response = supabase.rpc("get_toolset_secrets", {"p_toolset_id": toolset_id}).execute()
    return {row["key_name"]: row["secret_value"] for row in response.data or []}


async def fetch_agent_secrets(agent_id: str) -> dict[str, str]:
    response = supabase.rpc("get_agent_secrets", {"p_agent_id": agent_id}).execute()
    return {row["key_name"]: row["secret_value"] for row in response.data or []}


def _run_subprocess(
    code: str,
    tool_name: str,
    arguments: dict[str, Any],
    secrets: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    runner = textwrap.dedent("""
        import sys, json, os, traceback

        payload  = json.loads(sys.stdin.read())
        code     = payload["code"]
        fn_name  = payload["fn_name"]
        args     = payload["arguments"]

        namespace = {}
        try:
            exec(compile(code, "<tool>", "exec"), namespace)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"Compile error: {e}", "traceback": traceback.format_exc()}))
            sys.exit(0)

        fn = namespace.get(fn_name)
        if fn is None:
            print(json.dumps({"ok": False, "error": f"Function '{fn_name}' not found in tool code."}))
            sys.exit(0)

        try:
            result = fn(**args)
            print(json.dumps({"ok": True, "result": result}))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}))
    """)

    payload = json.dumps({"code": code, "fn_name": tool_name, "arguments": arguments})
    env = {**os.environ, **secrets}

    try:
        proc = subprocess.run(
            [sys.executable, "-c", runner],
            input=payload.encode(),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        raw = proc.stdout.decode().strip()
        if not raw:
            return {"ok": False, "error": proc.stderr.decode().strip() or "Tool produced no output.", "traceback": None}
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Tool execution timed out after {timeout}s.", "traceback": None}
    except Exception as e:
        return {"ok": False, "error": f"Executor error: {e}", "traceback": traceback.format_exc()}


async def run_tool(
    code: str,
    tool_name: str,
    arguments: dict[str, Any],
    secrets: dict[str, str],
    parameters: dict[str, Any] = {},
    timeout: int = 30,
) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_subprocess, code, tool_name, arguments, secrets, timeout,
    )