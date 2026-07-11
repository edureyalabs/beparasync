# beparasync/agent_runner.py
import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db import supabase
from executor import fetch_secrets, fetch_agent_secrets, run_tool, build_full_code
from llm.factory import get_llm_client
from sandbox_client import run_in_sandbox
from memory_manager import get_memory, build_memory_block, MEMORY_TOOL_DEF, apply_memory_action
from workspace_manager import pull_workspace, push_workspace, snapshot_hashes, get_task_md


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_step(run_id: str, steps: list, step: dict) -> list:
    steps = [*steps, step]
    supabase.from_("runs").update({"steps": steps}).eq("id", run_id).execute()
    return steps


def set_run_status(run_id: str, status: str, extra: dict = {}):
    supabase.from_("runs").update({"status": status, **extra}).eq("id", run_id).execute()


def load_agent(agent_id: str) -> dict:
    return supabase.from_("agents").select("*").eq("id", agent_id).single().execute().data


def load_contexts(agent_id: str) -> list[dict]:
    res = (
        supabase.from_("agent_contexts")
        .select("context_id, contexts(name, description, content)")
        .eq("agent_id", agent_id)
        .execute()
    )
    return [row["contexts"] for row in (res.data or []) if row.get("contexts")]


def load_tools(agent_id: str) -> list[dict]:
    res = supabase.from_("agent_toolsets").select("toolset_id").eq("agent_id", agent_id).execute()
    toolset_ids = [r["toolset_id"] for r in (res.data or [])]
    if not toolset_ids:
        return []
    return supabase.from_("tools").select("*").in_("toolset_id", toolset_ids).execute().data or []


def load_task(task_id: str) -> dict:
    return supabase.from_("tasks").select("*").eq("id", task_id).single().execute().data


def load_environment(agent_id: str) -> dict:
    res = supabase.from_("agent_environments").select("*").eq("agent_id", agent_id).execute()
    return res.data[0] if res.data else {}


def load_agent_platform_tools(agent_id: str) -> set[str]:
    res = supabase.from_("agent_platform_tools").select("platform_tool_key").eq("agent_id", agent_id).execute()
    return {r["platform_tool_key"] for r in (res.data or [])}


def push_file_to_storage(storage_path: str, content: bytes, mime_type: str = "application/octet-stream"):
    supabase.storage.from_("agent-files").upload(
        path=storage_path, file=content,
        file_options={"content-type": mime_type, "upsert": "true"},
    )


def write_execution_log(run_id: str, stdout: str, stderr: str, exit_code: int, duration_ms: int):
    if not run_id:
        return
    supabase.from_("execution_logs").upsert({
        "run_id":      run_id,
        "stdout":      stdout,
        "stderr":      stderr,
        "exit_code":   exit_code,
        "duration_ms": duration_ms,
    }, on_conflict="run_id").execute()


def build_tool_definitions(tools: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in tools]


def build_system_prompt(
    agent: dict,
    contexts: list[dict],
    memory: dict[str, str],
    agent_secret_keys: list[str],
    task_md: str,
    task: dict,
) -> str:
    parts = [agent["system_prompt"].strip()]

    if contexts:
        parts.append("\n\n---\n## Context\n")
        for ctx in contexts:
            parts.append(f"### {ctx['name']}")
            if ctx.get("description"):
                parts.append(f"_{ctx['description']}_")
            parts.append(ctx["content"])

    parts.append("\n\n" + build_memory_block(memory))

    if task_md:
        parts.append(f"\n\n══ TASK WORKSPACE CONTEXT ══\n{task_md}")
        parts.append(
            "\nYour task workspace is at /workspace/. "
            "All files from previous runs are available. "
            "After this run, update /workspace/TASK.md to reflect the current state."
        )
    else:
        parts.append(
            "\n\n══ TASK WORKSPACE ══\n"
            "This is the first run for this task. Your workspace at /workspace/ is empty. "
            "Build whatever structure the job requires. "
            "When done, create /workspace/TASK.md documenting what you built, "
            "the file structure, how to run it, and what remains to be done."
        )

    if agent_secret_keys:
        parts.append("\n\n══ AVAILABLE SECRETS ══")
        parts.append("Available as env vars via os.environ['KEY_NAME']:")
        for key in agent_secret_keys:
            parts.append(f"- `{key}`")

    return "\n".join(parts)


SANDBOX_TOOL_DEF = {
    "name": "execute_code",
    "description": (
        "Execute Python code in a secure sandbox with access to your task workspace at /workspace/. "
        "Files you create or modify persist between runs. "
        "Agent secrets available via os.environ['KEY_NAME']. "
        "Print results to stdout. Write files to /workspace/ to persist them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code":        {"type": "string", "description": "Python code to execute."},
            "entry_point": {"type": "string", "description": "Filename. Default: main.py"},
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
    agent_secrets: dict[str, str],
    environment: dict,
    workspace_dir: Path,
    steps: list,
) -> tuple[str, list]:
    code        = call["arguments"].get("code", "")
    entry_point = call["arguments"].get("entry_point", "main.py")

    # Encode workspace files for sandbox
    workspace_files: dict[str, bytes] = {}
    if workspace_dir.exists():
        for f in workspace_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(workspace_dir))
                workspace_files[rel] = f.read_bytes()

    flat_secrets: dict[str, str] = {}
    for d in all_secrets.values():
        flat_secrets.update(d)
    flat_secrets.update(agent_secrets)

    result = await run_in_sandbox(
        code=code,
        entry_point=entry_point,
        files={n: c for n, c in workspace_files.items()},
        env_vars=flat_secrets,
        packages=environment.get("packages", []),
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

    # Write output files back to local workspace dir for later push
    import base64
    for filename, b64 in result.get("output_files", {}).items():
        dest = workspace_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(b64))

    steps = append_step(run_id, steps, {
        "type":         "sandbox_output",
        "exit_code":    result.get("exit_code"),
        "stdout":       result.get("stdout", ""),
        "stderr":       result.get("stderr", ""),
        "duration_ms":  result.get("duration_ms", 0),
        "output_files": list(result.get("output_files", {}).keys()),
        "timestamp":    now_iso(),
    })

    if result.get("exit_code", -1) != 0:
        return_content = (
            f"Code execution failed (exit {result['exit_code']}).\n"
            f"stderr: {result.get('stderr', '')}\nstdout: {result.get('stdout', '')}"
        )
    else:
        return_content = result.get("stdout", "") or "Code executed successfully with no stdout output."

    return return_content, steps


async def run_agent_task(run_id: str, task_id: str, agent_id: str):
    steps: list[dict] = []
    workspace_dir = Path(tempfile.mkdtemp())

    try:
        set_run_status(run_id, "running", {"started_at": now_iso()})

        agent       = load_agent(agent_id)
        task        = load_task(task_id)
        contexts    = load_contexts(agent_id)
        tools       = load_tools(agent_id)
        environment = load_environment(agent_id)
        org_id      = task["org_id"]

        enabled_platform_tools = load_agent_platform_tools(agent_id)
        agent_secrets          = await fetch_agent_secrets(agent_id)
        memory                 = get_memory(agent_id)

        # Pull workspace from storage
        pull_workspace(org_id, agent_id, task_id, workspace_dir)
        pre_run_hashes = snapshot_hashes(workspace_dir)

        # Get TASK.md if it exists
        task_md = ""
        task_md_path = workspace_dir / "TASK.md"
        if task_md_path.exists():
            task_md = task_md_path.read_text(encoding="utf-8")

        system_prompt = build_system_prompt(
            agent=agent, contexts=contexts, memory=memory,
            agent_secret_keys=list(agent_secrets.keys()),
            task_md=task_md, task=task,
        )

        tool_defs = build_tool_definitions(tools) + [MEMORY_TOOL_DEF]
        if "execute_code" in enabled_platform_tools:
            tool_defs.append(SANDBOX_TOOL_DEF)

        tool_map    = {t["name"]: t for t in tools}
        toolset_ids = list({t["toolset_id"] for t in tools})
        all_secrets: dict[str, dict] = {}
        for tsid in toolset_ids:
            all_secrets[tsid] = await fetch_secrets(tsid)

        llm      = get_llm_client()
        messages: list[dict[str, Any]] = [{"role": "user", "content": task["instruction"]}]

        MAX_ITERATIONS = 20
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1

            current = supabase.from_("runs").select("status").eq("id", run_id).single().execute()
            if current.data and current.data.get("status") == "cancelled":
                set_run_status(run_id, "cancelled", {
                    "completed_at": now_iso(), "steps": steps,
                    "error": "Run was cancelled by user.",
                })
                return

            steps = append_step(run_id, steps, {
                "type": "llm_call", "iteration": iteration,
                "status": "pending", "timestamp": now_iso(),
            })

            llm_result = await llm.run(system_prompt, messages, tool_defs)

            if llm_result["type"] == "text":
                steps = append_step(run_id, steps, {
                    "type": "llm_response", "iteration": iteration,
                    "content": llm_result["content"], "timestamp": now_iso(),
                })
                set_run_status(run_id, "completed", {
                    "output": llm_result["content"],
                    "completed_at": now_iso(), "steps": steps,
                })
                break

            calls = llm_result["calls"]
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                    for c in calls
                ],
            })

            for call in calls:
                tool_name = call["name"]
                tool_args = call["arguments"]

                steps = append_step(run_id, steps, {
                    "type": "tool_call", "iteration": iteration,
                    "tool": tool_name, "arguments": tool_args,
                    "status": "running", "timestamp": now_iso(),
                })

                if tool_name == "memory":
                    tool_result_content = apply_memory_action(
                        agent_id=agent_id, org_id=org_id,
                        action=tool_args.get("action", ""),
                        target=tool_args.get("target", "agent"),
                        content=tool_args.get("content", ""),
                        old_text=tool_args.get("old_text", ""),
                    )

                elif tool_name == "execute_code":
                    tool_result_content, steps = await handle_sandbox_tool_call(
                        call=call, run_id=run_id, task_id=task_id,
                        agent_id=agent_id, org_id=org_id,
                        all_secrets=all_secrets, agent_secrets=agent_secrets,
                        environment=environment, workspace_dir=workspace_dir,
                        steps=steps,
                    )

                else:
                    tool = tool_map.get(tool_name)
                    if not tool:
                        tool_result_content = f"Error: tool '{tool_name}' not found."
                    else:
                        secrets   = all_secrets.get(tool["toolset_id"], {})
                        full_code = build_full_code(tool["name"], tool["parameters"], tool["code"])
                        res       = await run_tool(full_code, tool["name"], tool_args, secrets)
                        tool_result_content = (
                            f"Tool `{tool_name}` returned: {json.dumps(res['result'])}"
                            if res["ok"] else f"Tool `{tool_name}` failed: {res['error']}"
                        )

                steps = append_step(run_id, steps, {
                    "type": "tool_result", "iteration": iteration,
                    "tool": tool_name, "result": tool_result_content, "timestamp": now_iso(),
                })
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": tool_result_content})

        else:
            set_run_status(run_id, "failed", {
                "error": f"Max iterations ({MAX_ITERATIONS}) reached.",
                "completed_at": now_iso(), "steps": steps,
            })

    except Exception as e:
        import traceback
        set_run_status(run_id, "failed", {
            "error": str(e), "completed_at": now_iso(),
            "steps": [*steps, {
                "type": "error", "message": str(e),
                "traceback": traceback.format_exc(), "timestamp": now_iso(),
            }],
        })

    finally:
        # Push workspace changes back to storage regardless of success/failure
        try:
            push_workspace(org_id, agent_id, task_id, workspace_dir, pre_run_hashes)
        except Exception:
            pass
        shutil.rmtree(workspace_dir, ignore_errors=True)