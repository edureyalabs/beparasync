# beparasync/routes/chat.py

import json
import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from db import supabase
from llm.factory import get_llm_client
from executor import fetch_secrets, fetch_agent_secrets, run_tool, build_full_code
from memory_manager import (
    get_memory, build_memory_block, apply_memory_action,
    MEMORY_TOOL_DEF, SEARCH_HISTORY_TOOL_DEF,
)
from workspace_manager import get_task_md

router = APIRouter(prefix="/chat", tags=["chat"])

RECENT_MSG_COUNT = 20


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
            "instruction": {"type": "string", "description": "Detailed task instruction — what to do and how."},
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
            "task_id": {"type": "string", "description": "The task ID to run."},
        },
        "required": ["task_id"],
    },
}

LIST_TASKS_TOOL = {
    "name": "list_tasks",
    "description": "List all your tasks with their current status.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def handle_create_task(agent_id: str, org_id: str, name: str, instruction: str) -> str:
    res = supabase.from_("tasks").insert({
        "agent_id":     agent_id,
        "org_id":       org_id,
        "created_by":   agent_id,
        "name":         name,
        "instruction":  instruction,
        "trigger_type": "manual",
        "is_active":    True,
    }).select().single().execute()
    task = res.data
    return f"Task created: '{name}' (ID: {task['id']})"


def handle_run_task(task_id: str, org_id: str) -> str:
    from agent_runner import run_agent_task

    task_res = supabase.from_("tasks").select("*").eq("id", task_id).single().execute()
    if not task_res.data:
        return f"Task {task_id} not found."
    task = task_res.data

    run_res = supabase.from_("runs").insert({
        "task_id":      task_id,
        "agent_id":     task["agent_id"],
        "org_id":       org_id,
        "status":       "queued",
        "triggered_by": "agent_chat",
        "steps":        [],
    }).select().single().execute()
    run = run_res.data

    loop = asyncio.get_event_loop()
    loop.create_task(run_agent_task(run["id"], task_id, task["agent_id"]))

    return f"Task '{task['name']}' started. Run ID: {run['id']} — status: queued"


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
    lines = [f"- [{t['id'][:8]}] {t['name']} ({'active' if t['is_active'] else 'inactive'})" for t in res.data]
    return "\n".join(lines)


def build_chat_system_prompt(
    agent: dict,
    contexts: list[dict],
    memory: dict[str, str],
    task_md: str,
    active_task_id: str | None,
    agent_secret_keys: list[str],
) -> str:
    parts = [agent["system_prompt"].strip()]

    if contexts:
        parts.append("\n\n---\n## Context\n")
        for ctx in contexts:
            parts.append(f"### {ctx['name']}\n{ctx['content']}")

    parts.append("\n\n" + build_memory_block(memory))

    if task_md and active_task_id:
        parts.append(f"\n\n== ACTIVE TASK CONTEXT ==\n{task_md}")

    if agent_secret_keys:
        parts.append("\n\n== AVAILABLE SECRETS ==")
        parts.append("Available as env vars in execute_code sandbox via os.environ['KEY_NAME']:")
        for key in agent_secret_keys:
            parts.append(f"- `{key}`")

    parts.append(
        "\n\n== CAPABILITIES ==\n"
        "You can use tools to:\n"
        "- Execute code in a sandbox (execute_code — if enabled)\n"
        "- Create and run tasks (create_task, run_task, list_tasks)\n"
        "- Search past conversations (search_history)\n"
        "- Update your persistent memory (memory)\n"
        "When the user asks you to do something that requires structured work, create a task for it.\n"
        "Always update your AGENT.md memory with important facts you discover."
    )

    return "\n".join(parts)


def build_llm_messages(recent_messages: list[dict], user_message: str) -> list[dict]:
    messages = []
    for msg in recent_messages:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"] or ""})

        elif msg["role"] == "assistant":
            if msg.get("tool_calls"):
                normalized = []
                for tc in msg["tool_calls"]:
                    normalized.append({
                        "id":       tc.get("id", f"call_{len(normalized)}"),
                        "type":     "function",
                        "function": {
                            "name":      tc.get("name") or tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("arguments") or tc.get("function", {}).get("arguments", "{}"),
                        },
                    })
                messages.append({
                    "role":       "assistant",
                    "content":    None,
                    "tool_calls": normalized,
                })
            else:
                messages.append({"role": "assistant", "content": msg["content"] or ""})

        elif msg["role"] == "tool":
            meta = msg.get("meta") or {}
            messages.append({
                "role":         "tool",
                "tool_call_id": meta.get("tool_call_id", f"call_{len(messages)}"),
                "content":      msg["content"] or "",
            })

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

    task_md = ""
    if body.active_task_id:
        task_md = get_task_md(body.org_id, agent_id, body.active_task_id)

    system_prompt = build_chat_system_prompt(
        agent=agent,
        contexts=contexts,
        memory=memory,
        task_md=task_md,
        active_task_id=body.active_task_id,
        agent_secret_keys=list(agent_secrets.keys()),
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

    save_message(agent_id, body.org_id, "user", body.message, task_id=body.active_task_id)

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
            save_message(agent_id, body.org_id, "assistant", final_response, task_id=body.active_task_id)
            break

        calls = llm_result["calls"]

        # Save assistant message with tool calls
        save_message(
            agent_id, body.org_id, "assistant",
            tool_calls=[
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in calls
            ],
            task_id=body.active_task_id,
        )

        messages.append({
            "role":    "assistant",
            "content": None,
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

                result_content, _ = await handle_sandbox_tool_call(
                    call=call,
                    run_id=f"chat_{agent_id[:8]}",
                    task_id=body.active_task_id or agent_id,
                    agent_id=agent_id,
                    org_id=body.org_id,
                    all_secrets=all_secrets,
                    agent_secrets=agent_secrets,
                    environment=environment,
                    workspace_dir=__import__('pathlib').Path(__import__('tempfile').mkdtemp()),
                    steps=[],
                )

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
                tool_name=tool_name, task_id=body.active_task_id,
                meta={"tool_call_id": call["id"]},
            )

            messages.append({
                "role":         "tool",
                "tool_call_id": call["id"],
                "content":      result_content,
            })

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