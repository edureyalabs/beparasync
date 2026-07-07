# agent_runner.py
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from db import supabase
from executor import fetch_secrets, run_tool, build_full_code
from llm.factory import get_llm_client


# ─── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_step(run_id: str, steps: list, step: dict) -> list:
    steps = [*steps, step]
    supabase.from_("runs").update({"steps": steps}).eq("id", run_id).execute()
    return steps


def set_run_status(run_id: str, status: str, extra: dict = {}):
    supabase.from_("runs").update({"status": status, **extra}).eq("id", run_id).execute()


# ─── Load agent data ──────────────────────────────────────────────────────────

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
    """Returns all tools from all toolsets tagged to this agent."""
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


# ─── Build LLM tool definitions ───────────────────────────────────────────────

def build_tool_definitions(tools: list[dict]) -> list[dict]:
    return [
        {
            "name":        t["name"],
            "description": t["description"],
            "parameters":  t["parameters"],
        }
        for t in tools
    ]


# ─── Build system prompt ──────────────────────────────────────────────────────

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


# ─── Main runner ──────────────────────────────────────────────────────────────

async def run_agent_task(run_id: str, task_id: str, agent_id: str):
    """
    The full agentic loop:
      1. Load agent, contexts, tools, task
      2. Build system prompt (agent role + contexts injected)
      3. Start LLM call with tool definitions
      4. If LLM calls a tool → execute → feed result back → repeat
      5. When LLM returns text → write final output, mark complete
    Each step is written to runs.steps for polling.
    """
    steps: list[dict] = []

    try:
        # Mark as running
        set_run_status(run_id, "running", {"started_at": now_iso()})

        # Load everything
        agent    = load_agent(agent_id)
        task     = load_task(task_id)
        contexts = load_contexts(agent_id)
        tools    = load_tools(agent_id)

        system_prompt  = build_system_prompt(agent, contexts)
        tool_defs      = build_tool_definitions(tools)
        tool_map       = {t["name"]: t for t in tools}

        # Pre-fetch all secrets for all toolsets this agent uses
        toolset_ids = list({t["toolset_id"] for t in tools})
        all_secrets: dict[str, dict] = {}
        for tsid in toolset_ids:
            all_secrets[tsid] = await fetch_secrets(tsid)

        llm     = get_llm_client()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": task["instruction"]}
        ]

        MAX_ITERATIONS = 20  # guard against infinite loops
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1

            # ── LLM call ──
            steps = append_step(run_id, steps, {
                "type":      "llm_call",
                "iteration": iteration,
                "status":    "pending",
                "timestamp": now_iso(),
            })

            llm_result = await llm.run(system_prompt, messages, tool_defs)

            if llm_result["type"] == "text":
                # Final answer — done
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

            # ── Tool calls ──
            calls = llm_result["calls"]

            # Add assistant message with tool_calls to history
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

            # Execute each tool call
            for call in calls:
                tool_name = call["name"]
                tool_args = call["arguments"]
                tool      = tool_map.get(tool_name)

                steps = append_step(run_id, steps, {
                    "type":      "tool_call",
                    "iteration": iteration,
                    "tool":      tool_name,
                    "arguments": tool_args,
                    "status":    "running",
                    "timestamp": now_iso(),
                })

                if not tool:
                    tool_result_content = f"Error: tool '{tool_name}' not found."
                else:
                    secrets    = all_secrets.get(tool["toolset_id"], {})
                    full_code  = build_full_code(tool["name"], tool["parameters"], tool["code"])
                    exec_result = await run_tool(
                        code      = full_code,
                        tool_name = tool["name"],
                        arguments = tool_args,
                        secrets   = secrets,
                    )
                    if exec_result["ok"]:
                        result_value = exec_result["result"]
                        tool_result_content = (
                            f"Tool `{tool_name}` was called with arguments {json.dumps(tool_args)} "
                            f"and returned: {json.dumps(result_value)}"
                        )
                    else:
                        tool_result_content = (
                            f"Tool `{tool_name}` was called with arguments {json.dumps(tool_args)} "
                            f"but failed with error: {exec_result['error']}"
                        )

                steps = append_step(run_id, steps, {
                    "type":      "tool_result",
                    "iteration": iteration,
                    "tool":      tool_name,
                    "result":    tool_result_content,
                    "timestamp": now_iso(),
                })

                # Feed result back to LLM
                messages.append({
                    "role":         "tool",
                    "tool_call_id": call["id"],
                    "content":      tool_result_content,
                })

        # Hit iteration limit
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