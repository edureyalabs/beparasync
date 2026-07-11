# beparasync/memory_manager.py

from db import supabase

AGENT_MEMORY_LIMIT = 2000
USER_MEMORY_LIMIT  = 1500

MEMORY_TOOL_DEF = {
    "name": "memory",
    "description": (
        "Manage your persistent memory. "
        "Use 'add' to save a new fact, 'replace' to update an existing one using a unique substring, "
        "'remove' to delete one. "
        "target='agent' updates your agent notes (environment facts, task learnings, capabilities). "
        "target='user' updates the user profile (preferences, background, communication style). "
        "Keep entries dense and factual. No timestamps. No fluff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action":   {"type": "string", "enum": ["add", "replace", "remove"], "description": "Operation to perform."},
            "target":   {"type": "string", "enum": ["agent", "user"], "description": "Which memory file to update."},
            "content":  {"type": "string", "description": "The fact to add or the replacement content."},
            "old_text": {"type": "string", "description": "Unique substring of the entry to replace or remove (required for replace/remove)."},
        },
        "required": ["action", "target"],
    },
}

SEARCH_HISTORY_TOOL_DEF = {
    "name": "search_history",
    "description": (
        "Search past conversation history beyond the last 20 messages. "
        "Use when you need to recall something discussed earlier. "
        "Returns relevant message excerpts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for in conversation history."},
        },
        "required": ["query"],
    },
}


def get_memory(agent_id: str) -> dict[str, str]:
    res = (
        supabase.from_("agent_memory")
        .select("memory_type, content")
        .eq("agent_id", agent_id)
        .execute()
    )
    result = {"agent": "", "user": ""}
    for row in (res.data or []):
        result[row["memory_type"]] = row["content"]
    return result


def update_memory(agent_id: str, org_id: str, memory_type: str, content: str):
    supabase.from_("agent_memory").upsert({
        "agent_id":    agent_id,
        "org_id":      org_id,
        "memory_type": memory_type,
        "content":     content,
        "char_count":  len(content),
        "updated_at":  "now()",
    }, on_conflict="agent_id,memory_type").execute()


def apply_memory_action(
    agent_id: str,
    org_id: str,
    action: str,
    target: str,
    content: str = "",
    old_text: str = "",
) -> str:
    memory = get_memory(agent_id)
    current = memory.get(target, "")
    limit   = AGENT_MEMORY_LIMIT if target == "agent" else USER_MEMORY_LIMIT

    if action == "add":
        if not content.strip():
            return "Error: content is required for add."
        new_content = (current + "\n§\n" + content.strip()).strip()
        if len(new_content) > limit:
            used = len(current)
            return (
                f"Memory full ({used}/{limit} chars). "
                f"Current entries:\n{current}\n\n"
                f"Use 'replace' to consolidate entries before adding new ones."
            )
        update_memory(agent_id, org_id, target, new_content)
        return f"Added to {target} memory. ({len(new_content)}/{limit} chars)"

    elif action == "replace":
        if not old_text.strip():
            return "Error: old_text is required for replace."
        if old_text not in current:
            return f"Error: substring '{old_text}' not found in {target} memory."
        matches = current.split("§")
        matched = [e for e in matches if old_text in e]
        if len(matched) > 1:
            return f"Error: '{old_text}' matches {len(matched)} entries. Use a more specific substring."
        new_content = current.replace(matched[0], f"\n{content.strip()}\n").strip()
        new_content = "\n§\n".join(e.strip() for e in new_content.split("§") if e.strip())
        update_memory(agent_id, org_id, target, new_content)
        return f"Replaced in {target} memory. ({len(new_content)}/{limit} chars)"

    elif action == "remove":
        if not old_text.strip():
            return "Error: old_text is required for remove."
        if old_text not in current:
            return f"Error: substring '{old_text}' not found in {target} memory."
        entries = [e.strip() for e in current.split("§")]
        remaining = [e for e in entries if old_text not in e]
        new_content = "\n§\n".join(remaining)
        update_memory(agent_id, org_id, target, new_content)
        return f"Removed from {target} memory. ({len(new_content)}/{limit} chars)"

    return f"Unknown action: {action}"


def build_memory_block(memory: dict[str, str]) -> str:
    agent_mem = memory.get("agent", "").strip()
    user_mem  = memory.get("user", "").strip()

    agent_pct = int(len(agent_mem) / AGENT_MEMORY_LIMIT * 100) if agent_mem else 0
    user_pct  = int(len(user_mem) / USER_MEMORY_LIMIT * 100)   if user_mem else 0

    parts = []

    if agent_mem:
        parts.append(
            f"══ AGENT MEMORY [{agent_pct}% — {len(agent_mem)}/{AGENT_MEMORY_LIMIT} chars] ══\n"
            f"{agent_mem}"
        )
    else:
        parts.append(
            f"══ AGENT MEMORY [0% — 0/{AGENT_MEMORY_LIMIT} chars] ══\n"
            "(empty — use memory tool to save important facts as you learn them)"
        )

    if user_mem:
        parts.append(
            f"══ USER PROFILE [{user_pct}% — {len(user_mem)}/{USER_MEMORY_LIMIT} chars] ══\n"
            f"{user_mem}"
        )
    else:
        parts.append(
            f"══ USER PROFILE [0% — 0/{USER_MEMORY_LIMIT} chars] ══\n"
            "(empty — save user preferences and background as you learn them)"
        )

    return "\n\n".join(parts)