# beparasync/agent_runner.py

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from db import supabase
from executor import fetch_secrets, run_tool, build_full_code
from llm.factory import get_llm_client
from sandbox_client import run_in_sandbox


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_step(run_id: str, steps: list, step: dict) -> list:
    steps = [*steps, step]
    supabase.from_("runs").update({"steps": steps}).eq("id", run_id).execute()
    return steps


def set_run_status(run_id: str, status: str, extra: dict = {}):
    supabase.from_("runs").update({"status": status, **extra}).eq("id", run_id).execute()


def load_agent(agent_id: str) -> dict:
    res = supabase.from_("agents").select("*").eq("id", agent_id).single().execute()
    return res.data


def load_contexts(agent_id: str) -> list[dict]:
    res = (
        supabase.from_("agent_contexts")
        .select("context_id, contexts(name, description, content)")
        .eq("agent_id", agent_id)
        .execute()
    )
    return [row["contexts"] for row in (res.data or []) if row.get("contexts")]


def load_tools(agent_id: str) -> list[dict]:
    res = (
        supabase.from_("agent_toolsets")
        .select("toolset_id")
        .eq("agent_id", agent_id)
        .execute()
    )
    toolset_ids = [row["toolset_id"] for row in (res.data or [])]
    if not toolset_ids:
        return []

    tools_res = (
        supabase.from_("tools")
        .select("*")
        .in_("toolset_id", toolset_ids)
        .execute()
    )
    return tools_res.data or []


def load_task(task_id: str) -> dict:
    res = supabase.from_("tasks").select("*").eq("id", task_id).single().execute()
    return res.data


def load_task_files(task_id: str) -> list[dict]:
    res = (
        supabase.from_("task_files")
        .select("filename, storage_path, mime_type")
        .eq("task_id", task_id)
        .execute()
    )
    return res.data or []


def load_environment(agent_id: str) -> dict:
    res = supabase.from_("agent_environments").select("*").eq("agent_id", agent_id).execute()
    return res.data[0] if res.data else {}


def fetch_file_from_storage(storage_path: str) -> bytes:
    result = supabase.storage.from_("agent-files").download(storage_path)
    return result


def push_file_to_storage(storage_path: str, content: bytes, mime_type: str = "application/octet-stream"):
    supabase.storage.from_("agent-files").upload(
        path=storage_path,
        file=content,
        file_options={"content-type": mime_type, "upsert": "true"},
    )


def write_execution_log(run_id: str, stdout: str, stderr: str, exit_code: int, duration_ms: int):
    supabase.from_("execution_logs").upsert({
        "run_id":      run_id,
        "stdout":      stdout,
        "stderr":      stderr,
        "exit_code":   exit_code,
        "duration_ms": duration_ms,
    }, on_conflict="run_id").execute()


def build_tool_definitions(tools: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
        for t in tools
    ]


def build_system_prompt(agent: dict, contexts: list[dict]) -> str:
    parts = [agent["system_prompt"].strip()]
    if contexts:
        parts.append("\n\n---\n## Context\n")
        for ctx in contexts:
            parts.append(f"### {ctx['name']}")
            if ctx.get("description"):
                parts.append(f"_{ctx['description']}_")
            parts.append(ctx["content"])
    return "\n".join(parts)


# ─── Sandbox tool: execute_code ───────────────────────────────────────────────
# A built-in tool the LLM can call to run code in the sandbox.
# It is injected into the tool list automatically — agents don't need to define it.

SANDBOX_TOOL_DEF = {
    "name": "execute_code",
    "description": (
        "Execute Python code in a secure sandbox. "
        "The code runs in an isolated container with access to the task's workspace files. "
        "Use this for computation, file processing, data analysis, or any task requiring code execution. "
        "Returns stdout output. Write results to files if you need to persist them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python code to execute.",
            },
            "entry_point": {
                "type": "string",
                "description": "Filename for the script, default is main.py",
            },
        },
        "required": ["code"],
    },
}


async def handle_sandbox_tool_call(
    call: dict,
    run_id: str,
    task_id: str,
    agent_id: str,
    org_id: str,
    all_secrets: dict[str, dict],
    environment: dict,
    steps: list,
) -> tuple[str, list]:
    code       = call["arguments"].get("code", "")
    entry_point = call["arguments"].get("entry_point", "main.py")

    # Pull task files from storage into the sandbox
    task_files_meta = load_task_files(task_id)
    workspace_files: dict[str, bytes] = {}
    for f in task_files_meta:
        try:
            workspace_files[f["filename"]] = fetch_file_from_storage(f["storage_path"])
        except Exception:
            pass

    # Flatten all toolset secrets into one dict for env injection
    flat_secrets: dict[str, str] = {}
    for secrets_dict in all_secrets.values():
        flat_secrets.update(secrets_dict)

    packages = environment.get("packages", [])

    result = await run_in_sandbox(
        code=code,
        entry_point=entry_point,
        files={name: content for name, content in workspace_files.items()},
        env_vars=flat_secrets,
        packages=packages,
        agent_id=agent_id,
        timeout=60,
    )

    write_execution_log(
        run_id=run_id,
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        exit_code=result.get("exit_code", -1),
        duration_ms=result.get("duration_ms", 0),
    )

    # Push any new output files back to storage
    for filename, b64_content in result.get("output_files", {}).items():
        import base64
        content = base64.b64decode(b64_content)
        storage_path = f"{org_id}/{agent_id}/{task_id}/{filename}"
        push_file_to_storage(storage_path, content)

        supabase.from_("task_files").upsert({
            "task_id":      task_id,
            "agent_id":     agent_id,
            "org_id":       org_id,
            "filename":     filename,
            "storage_path": storage_path,
            "size_bytes":   len(content),
            "uploaded_by":  "agent",
        }).execute()

    steps = append_step(run_id, steps, {
        "type":        "sandbox_output",
        "exit_code":   result.get("exit_code"),
        "stdout":      result.get("stdout", ""),
        "stderr":      result.get("stderr", ""),
        "duration_ms": result.get("duration_ms", 0),
        "output_files": list(result.get("output_files", {}).keys()),
        "timestamp":   now_iso(),
    })

    if result.get("exit_code", -1) != 0:
        return_content = (
            f"Code execution failed (exit {result['exit_code']}).\n"
            f"stderr: {result.get('stderr', '')}\n"
            f"stdout: {result.get('stdout', '')}"
        )
    else:
        return_content = result.get("stdout", "") or "Code executed successfully with no stdout output."

    return return_content, steps


# ─── Main runner ──────────────────────────────────────────────────────────────

async def run_agent_task(run_id: str, task_id: str, agent_id: str):
    steps: list[dict] = []

    try:
        set_run_status(run_id, "running", {"started_at": now_iso()})

        agent       = load_agent(agent_id)
        task        = load_task(task_id)
        contexts    = load_contexts(agent_id)
        tools       = load_tools(agent_id)
        environment = load_environment(agent_id)

        org_id = task["org_id"]

        system_prompt = build_system_prompt(agent, contexts)
        tool_defs     = build_tool_definitions(tools) + [SANDBOX_TOOL_DEF]
        tool_map      = {t["name"]: t for t in tools}

        toolset_ids = list({t["toolset_id"] for t in tools})
        all_secrets: dict[str, dict] = {}
        for tsid in toolset_ids:
            all_secrets[tsid] = await fetch_secrets(tsid)

        llm      = get_llm_client()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": task["instruction"]}
        ]

        MAX_ITERATIONS = 20
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1

            current = supabase.from_("runs").select("status").eq("id", run_id).single().execute()
            if current.data and current.data.get("status") == "cancelled":
                set_run_status(run_id, "cancelled", {
                    "completed_at": now_iso(),
                    "steps": steps,
                    "error": "Run was cancelled by user.",
                })
                return

            steps = append_step(run_id, steps, {
                "type":      "llm_call",
                "iteration": iteration,
                "status":    "pending",
                "timestamp": now_iso(),
            })

            llm_result = await llm.run(system_prompt, messages, tool_defs)

            if llm_result["type"] == "text":
                steps = append_step(run_id, steps, {
                    "type":      "llm_response",
                    "iteration": iteration,
                    "content":   llm_result["content"],
                    "timestamp": now_iso(),
                })
                set_run_status(run_id, "completed", {
                    "output":       llm_result["content"],
                    "completed_at": now_iso(),
                    "steps":        steps,
                })
                return

            calls = llm_result["calls"]

            messages.append({
                "role":       "assistant",
                "content":    None,
                "tool_calls": [
                    {
                        "id":       c["id"],
                        "type":     "function",
                        "function": {
                            "name":      c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for c in calls
                ],
            })

            for call in calls:
                tool_name = call["name"]
                tool_args = call["arguments"]

                steps = append_step(run_id, steps, {
                    "type":      "tool_call",
                    "iteration": iteration,
                    "tool":      tool_name,
                    "arguments": tool_args,
                    "status":    "running",
                    "timestamp": now_iso(),
                })

                # ── Built-in sandbox tool ──
                if tool_name == "execute_code":
                    tool_result_content, steps = await handle_sandbox_tool_call(
                        call=call,
                        run_id=run_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        org_id=org_id,
                        all_secrets=all_secrets,
                        environment=environment,
                        steps=steps,
                    )
                else:
                    # ── Regular tool call (existing path, untouched) ──
                    tool = tool_map.get(tool_name)
                    if not tool:
                        tool_result_content = f"Error: tool '{tool_name}' not found."
                    else:
                        secrets    = all_secrets.get(tool["toolset_id"], {})
                        full_code  = build_full_code(tool["name"], tool["parameters"], tool["code"])
                        exec_result = await run_tool(
                            code=full_code,
                            tool_name=tool["name"],
                            arguments=tool_args,
                            secrets=secrets,
                        )
                        if exec_result["ok"]:
                            tool_result_content = (
                                f"Tool `{tool_name}` returned: {json.dumps(exec_result['result'])}"
                            )
                        else:
                            tool_result_content = (
                                f"Tool `{tool_name}` failed: {exec_result['error']}"
                            )

                steps = append_step(run_id, steps, {
                    "type":      "tool_result",
                    "iteration": iteration,
                    "tool":      tool_name,
                    "result":    tool_result_content,
                    "timestamp": now_iso(),
                })

                messages.append({
                    "role":         "tool",
                    "tool_call_id": call["id"],
                    "content":      tool_result_content,
                })

        set_run_status(run_id, "failed", {
            "error":        f"Max iterations ({MAX_ITERATIONS}) reached without a final answer.",
            "completed_at": now_iso(),
            "steps":        steps,
        })

    except Exception as e:
        import traceback
        set_run_status(run_id, "failed", {
            "error":        str(e),
            "completed_at": now_iso(),
            "steps":        [*steps, {
                "type":      "error",
                "message":   str(e),
                "traceback": traceback.format_exc(),
                "timestamp": now_iso(),
            }],
        })