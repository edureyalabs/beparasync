# beparasync/routes/chat.py

import json
import asyncio
import shutil
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any
from pathlib import Path
import tempfile
import os

from db import supabase
from llm.factory import get_llm_client
from executor import fetch_secrets, fetch_agent_secrets, run_tool, build_full_code
from memory_manager import (
    get_memory, build_memory_block, apply_memory_action,
    MEMORY_TOOL_DEF, SEARCH_HISTORY_TOOL_DEF,
)
from workspace_manager import get_task_md, pull_workspace, push_workspace, snapshot_hashes
from web_search import run_web_search
from browser_client import run_in_browser
from app_context import get_apps_md, build_app_context_block

router = APIRouter(prefix="/chat", tags=["chat"])

RECENT_MSG_COUNT = 20
FRONTEND_URL = os.getenv("FRONTEND_URL", "")  # e.g. https://parasync.vercel.app


class ChatRequest(BaseModel):
    message: str
    org_id: str
    active_task_id: str | None = None


def load_agent(agent_id: str) -> dict:
    return supabase.from_("agents").select("*").eq("id", agent_id).single().execute().data


def load_contexts(agent_id: str) -> list[dict]:
    res = (
        supabase.from_("agent_contexts")
        .select("context_id, contexts(name, content)")
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


def load_platform_tools(agent_id: str) -> set[str]:
    res = supabase.from_("agent_platform_tools").select("platform_tool_key").eq("agent_id", agent_id).execute()
    return {r["platform_tool_key"] for r in (res.data or [])}


def load_recent_messages(agent_id: str, limit: int = RECENT_MSG_COUNT) -> list[dict]:
    res = (
        supabase.from_("conversations")
        .select("role, content, tool_calls, tool_name, meta, created_at")
        .eq("agent_id", agent_id)
        .neq("role", "summary")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(res.data or []))


def resolve_task_id(agent_id: str, active_task_id: str | None) -> str | None:
    if active_task_id:
        return active_task_id
    res = (
        supabase.from_("tasks")
        .select("id")
        .eq("agent_id", agent_id)
        .eq("is_active", True)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0]["id"] if res.data else None


def search_history(agent_id: str, query: str) -> str:
    res = (
        supabase.from_("conversations")
        .select("role, content, created_at")
        .eq("agent_id", agent_id)
        .ilike("content", f"%{query}%")
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    if not res.data:
        return f"No conversation history found matching '{query}'."
    lines = []
    for row in res.data:
        ts   = row["created_at"][:10]
        role = row["role"].upper()
        snip = (row["content"] or "")[:200]
        lines.append(f"[{ts}] {role}: {snip}")
    return "\n".join(lines)


def save_message(agent_id: str, org_id: str, role: str, content: str = "",
                 tool_calls: Any = None, tool_name: str = "",
                 task_id: str = None, meta: dict = None):
    supabase.from_("conversations").insert({
        "agent_id":   agent_id,
        "org_id":     org_id,
        "role":       role,
        "content":    content,
        "tool_calls": tool_calls,
        "tool_name":  tool_name or None,
        "task_id":    task_id,
        "meta":       meta or {},
    }).execute()


CREATE_TASK_TOOL = {
    "name": "create_task",
    "description": "Create a new task for yourself. Use when the user's request requires a structured, executable job.",
    "parameters": {
        "type": "object",
        "properties": {
            "name":        {"type": "string", "description": "Short task name."},
            "instruction": {"type": "string", "description": "Detailed task instruction."},
        },
        "required": ["name", "instruction"],
    },
}

RUN_TASK_TOOL = {
    "name": "run_task",
    "description": "Trigger a task run. Returns a run_id to track progress.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The full UUID task ID to run."},
        },
        "required": ["task_id"],
    },
}

LIST_TASKS_TOOL = {
    "name": "list_tasks",
    "description": "List all your tasks with their full task_id UUIDs and status.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

WEB_SEARCH_TOOL_DEF = {
    "name": "web_search",
    "description": (
        "Search the web for current information. "
        "Returns titles, URLs, and descriptions of the top results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "count": {"type": "integer", "description": "Number of results (1-10). Default: 5."},
        },
        "required": ["query"],
    },
}

BROWSE_WEB_TOOL_DEF = {
    "name": "browse_web",
    "description": (
        "Browse the web using a full AI-powered browser. "
        "Can navigate pages, click buttons, fill forms, and extract content. "
        "Use when web_search is not enough. Slower than web_search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task":      {"type": "string", "description": "Natural language instruction for the browser."},
            "start_url": {"type": "string", "description": "Optional starting URL."},
            "timeout":   {"type": "integer", "description": "Max seconds. Default: 120."},
        },
        "required": ["task"],
    },
}


def handle_create_task(agent_id: str, org_id: str, name: str, instruction: str) -> str:
    org_res  = supabase.from_("organizations").select("owner_id").eq("id", org_id).execute()
    owner_id = org_res.data[0]["owner_id"] if org_res.data else None
    if not owner_id:
        return "Could not create task: unable to resolve org owner."
    res = supabase.from_("tasks").insert({
        "agent_id":     agent_id,
        "org_id":       org_id,
        "created_by":   owner_id,
        "name":         name,
        "instruction":  instruction,
        "trigger_type": "manual",
        "is_active":    True,
    }).execute()
    rows = res.data or []
    tid  = rows[0]["id"] if rows else "unknown"
    return f"Task created: '{name}' (ID: {tid})"


def handle_run_task(task_id: str, org_id: str) -> str:
    import re
    from agent_runner import run_agent_task
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', task_id or ''):
        return f"Invalid task_id '{task_id}'. Use list_tasks to get the full UUID."
    task_res = supabase.from_("tasks").select("*").eq("id", task_id).execute()
    if not task_res.data:
        return f"Task {task_id} not found."
    task    = task_res.data[0]
    run_res = supabase.from_("runs").insert({
        "task_id":      task_id,
        "agent_id":     task["agent_id"],
        "org_id":       org_id,
        "status":       "queued",
        "triggered_by": "agent_chat",
        "steps":        [],
    }).execute()
    run_rows = run_res.data or []
    run_id   = run_rows[0]["id"] if run_rows else None
    if not run_id:
        return f"Task '{task['name']}' queued but run ID unavailable."
    loop = asyncio.get_event_loop()
    loop.create_task(run_agent_task(run_id, task_id, task["agent_id"]))
    return f"Task '{task['name']}' started. Run ID: {run_id} — status: queued"


def handle_list_tasks(agent_id: str) -> str:
    res = (
        supabase.from_("tasks")
        .select("id, name, is_active, created_at")
        .eq("agent_id", agent_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )
    if not res.data:
        return "No tasks yet."
    lines = [
        f"- {t['name']} | task_id: {t['id']} | {'active' if t['is_active'] else 'inactive'}"
        for t in res.data
    ]
    return "\n".join(lines)


def build_chat_system_prompt(
    agent: dict,
    contexts: list[dict],
    memory: dict[str, str],
    task_md: str,
    apps_md: str,
    resolved_task_id: str | None,
    agent_secret_keys: list[str],
    agent_id: str = "",
    task_id: str = "",
) -> str:
    parts = [agent["system_prompt"].strip()]

    if contexts:
        parts.append("\n\n---\n## Context\n")
        for ctx in contexts:
            parts.append(f"### {ctx['name']}\n{ctx['content']}")

    parts.append("\n\n" + build_memory_block(memory))

    if task_md and resolved_task_id:
        parts.append(f"\n\n== ACTIVE TASK CONTEXT ==\n{task_md}")

    # App builder context
    parts.append(f"\n\n{build_app_context_block(apps_md)}")

    # Always inject the exact app URL format so agent gives correct links
    frontend = FRONTEND_URL.rstrip("/")
    if agent_id and task_id:
        parts.append(
            f"\n\n== APP URLS ==\n"
            f"When you build an app named {{app-name}}, tell the user the EXACT shareable URL:\n"
            f"{frontend}/apps/{agent_id}/{task_id}/{{app-name}}\n"
            f"Always use this full URL. Never use a partial path like /apps/app-name."
        )

    if agent_secret_keys:
        parts.append("\n\n== AVAILABLE SECRETS ==")
        parts.append("Available as env vars in execute_code sandbox via os.environ['KEY_NAME']:")
        for key in agent_secret_keys:
            parts.append(f"- `{key}`")

    parts.append(
        "\n\n== CAPABILITIES ==\n"
        "- Execute code in a sandbox (execute_code — if enabled)\n"
        "- Search the web (web_search — if enabled)\n"
        "- Browse the web with a full browser (browse_web — if enabled)\n"
        "- Build interactive web apps (write to /workspace/apps/{name}/ via execute_code)\n"
        "- Create and run tasks (create_task, run_task, list_tasks)\n"
        "- Search past conversations (search_history)\n"
        "- Update persistent memory (memory)\n"
        "Always use list_tasks to get full task UUIDs before calling run_task."
    )

    return "\n".join(parts)


def build_llm_messages(recent_messages: list[dict], user_message: str) -> list[dict]:
    covered = set()
    for msg in recent_messages:
        if msg["role"] == "tool":
            tid = (msg.get("meta") or {}).get("tool_call_id")
            if tid:
                covered.add(tid)

    messages = []
    for msg in recent_messages:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"] or ""})
        elif msg["role"] == "assistant":
            if msg.get("tool_calls"):
                normalized = []
                for tc in msg["tool_calls"]:
                    tc_id   = tc.get("id", "")
                    tc_name = tc.get("name") or tc.get("function", {}).get("name", "")
                    tc_args = tc.get("arguments") or tc.get("function", {}).get("arguments", "{}")
                    if not tc_name or tc_id not in covered:
                        continue
                    normalized.append({
                        "id": tc_id, "type": "function",
                        "function": {"name": tc_name, "arguments": tc_args},
                    })
                if normalized:
                    messages.append({"role": "assistant", "content": None, "tool_calls": normalized})
            else:
                messages.append({"role": "assistant", "content": msg["content"] or ""})
        elif msg["role"] == "tool":
            tool_call_id = (msg.get("meta") or {}).get("tool_call_id")
            if not tool_call_id:
                continue
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": msg["content"] or ""})

    messages.append({"role": "user", "content": user_message})
    return messages


@router.post("/{agent_id}")
async def chat(agent_id: str, body: ChatRequest):
    agent          = load_agent(agent_id)
    contexts       = load_contexts(agent_id)
    tools          = load_tools(agent_id)
    platform_tools = load_platform_tools(agent_id)
    memory         = get_memory(agent_id)
    recent_msgs    = load_recent_messages(agent_id)
    agent_secrets  = await fetch_agent_secrets(agent_id)

    resolved_task_id = resolve_task_id(agent_id, body.active_task_id)

    task_md = ""
    if resolved_task_id:
        task_md = get_task_md(body.org_id, agent_id, resolved_task_id)

    apps_md = get_apps_md(body.org_id, agent_id, resolved_task_id) if resolved_task_id else ""

    system_prompt = build_chat_system_prompt(
        agent=agent,
        contexts=contexts,
        memory=memory,
        task_md=task_md,
        apps_md=apps_md,
        resolved_task_id=resolved_task_id,
        agent_secret_keys=list(agent_secrets.keys()),
        agent_id=agent_id,
        task_id=resolved_task_id or "",
    )

    tool_map    = {t["name"]: t for t in tools}
    toolset_ids = list({t["toolset_id"] for t in tools})
    all_secrets: dict[str, dict] = {}
    for tsid in toolset_ids:
        all_secrets[tsid] = await fetch_secrets(tsid)

    tool_defs = [
        {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
        for t in tools
    ]
    tool_defs += [MEMORY_TOOL_DEF, SEARCH_HISTORY_TOOL_DEF, CREATE_TASK_TOOL, RUN_TASK_TOOL, LIST_TASKS_TOOL]

    if "execute_code" in platform_tools:
        from agent_runner import SANDBOX_TOOL_DEF
        tool_defs.append(SANDBOX_TOOL_DEF)
    if "web_search" in platform_tools:
        tool_defs.append(WEB_SEARCH_TOOL_DEF)
    if "browse_web" in platform_tools:
        tool_defs.append(BROWSE_WEB_TOOL_DEF)

    save_message(agent_id, body.org_id, "user", body.message, task_id=resolved_task_id)

    llm      = get_llm_client()
    messages = build_llm_messages(recent_msgs, body.message)

    MAX_ITERATIONS = 10
    iteration      = 0
    final_response = ""

    while iteration < MAX_ITERATIONS:
        iteration += 1
        llm_result = await llm.run(system_prompt, messages, tool_defs)

        if llm_result["type"] == "text":
            final_response = llm_result["content"]
            save_message(agent_id, body.org_id, "assistant", final_response, task_id=resolved_task_id)
            break

        calls = llm_result["calls"]

        save_message(
            agent_id, body.org_id, "assistant",
            tool_calls=[
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in calls
            ],
            task_id=resolved_task_id,
        )

        messages.append({
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in calls
            ],
        })

        for call in calls:
            tool_name      = call["name"]
            tool_args      = call["arguments"]
            result_content = ""

            if tool_name == "memory":
                result_content = apply_memory_action(
                    agent_id=agent_id, org_id=body.org_id,
                    action=tool_args.get("action", ""),
                    target=tool_args.get("target", "agent"),
                    content=tool_args.get("content", ""),
                    old_text=tool_args.get("old_text", ""),
                )

            elif tool_name == "search_history":
                result_content = search_history(agent_id, tool_args.get("query", ""))

            elif tool_name == "create_task":
                result_content = handle_create_task(
                    agent_id, body.org_id,
                    tool_args.get("name", ""),
                    tool_args.get("instruction", ""),
                )

            elif tool_name == "run_task":
                result_content = handle_run_task(tool_args.get("task_id", ""), body.org_id)

            elif tool_name == "list_tasks":
                result_content = handle_list_tasks(agent_id)

            elif tool_name == "execute_code":
                from agent_runner import handle_sandbox_tool_call

                environment = {}
                env_res = supabase.from_("agent_environments").select("*").eq("agent_id", agent_id).execute()
                if env_res.data:
                    environment = env_res.data[0]

                workspace_dir = Path(tempfile.mkdtemp())
                try:
                    # Pull existing workspace so agent has all previous files
                    if resolved_task_id:
                        pull_workspace(body.org_id, agent_id, resolved_task_id, workspace_dir)
                    pre_hashes = snapshot_hashes(workspace_dir)

                    result_content, _ = await handle_sandbox_tool_call(
                        call=call,
                        run_id=None,
                        task_id=resolved_task_id or agent_id,
                        agent_id=agent_id,
                        org_id=body.org_id,
                        all_secrets=all_secrets,
                        agent_secrets=agent_secrets,
                        environment=environment,
                        workspace_dir=workspace_dir,
                        steps=[],
                    )

                    # Push new/changed files back to storage
                    if resolved_task_id:
                        import logging
                        files_in_workspace = list(workspace_dir.rglob("*"))
                        logging.warning(f"[chat] workspace files before push: {[str(f.relative_to(workspace_dir)) for f in files_in_workspace if f.is_file()]}")
                        push_workspace(body.org_id, agent_id, resolved_task_id, workspace_dir, pre_hashes)
                        logging.warning(f"[chat] push_workspace completed")

                finally:
                    shutil.rmtree(workspace_dir, ignore_errors=True)

            elif tool_name == "web_search":
                result_content = await run_web_search(
                    tool_args.get("query", ""), tool_args.get("count", 5)
                )

            elif tool_name == "browse_web":
                flat_secrets: dict[str, str] = {}
                for d in all_secrets.values():
                    flat_secrets.update(d)
                flat_secrets.update(agent_secrets)
                result = await run_in_browser(
                    task=tool_args.get("task", ""),
                    start_url=tool_args.get("start_url"),
                    env_vars=flat_secrets,
                    timeout=tool_args.get("timeout", 120),
                )
                if result["ok"]:
                    steps_summary = ", ".join(result.get("steps", [])[:5])
                    result_content = f"Browser task completed.\nSteps: {steps_summary}\n\nResult:\n{result['result']}"
                else:
                    result_content = f"Browser task failed: {result.get('error', 'Unknown error')}"

            else:
                tool = tool_map.get(tool_name)
                if not tool:
                    result_content = f"Tool '{tool_name}' not found."
                else:
                    secrets   = all_secrets.get(tool["toolset_id"], {})
                    full_code = build_full_code(tool["name"], tool["parameters"], tool["code"])
                    res       = await run_tool(full_code, tool["name"], tool_args, secrets)
                    result_content = json.dumps(res["result"]) if res["ok"] else f"Error: {res['error']}"

            save_message(
                agent_id, body.org_id, "tool", result_content,
                tool_name=tool_name, task_id=resolved_task_id,
                meta={"tool_call_id": call["id"]},
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_content})

    return {"response": final_response, "iterations": iteration}


@router.get("/{agent_id}/history")
def get_history(agent_id: str, limit: int = 50, offset: int = 0):
    res = (
        supabase.from_("conversations")
        .select("id, role, content, tool_calls, tool_name, task_id, created_at")
        .eq("agent_id", agent_id)
        .neq("role", "summary")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return list(reversed(res.data or []))


@router.get("/{agent_id}/memory")
def get_agent_memory(agent_id: str):
    return get_memory(agent_id)


@router.delete("/{agent_id}/history")
def clear_history(agent_id: str):
    supabase.from_("conversations").delete().eq("agent_id", agent_id).execute()
    return {"ok": True}