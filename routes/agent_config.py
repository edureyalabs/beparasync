# beparasync/routes/agent_config.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import supabase

router = APIRouter(prefix="/agents", tags=["agent-config"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class AddSecretRequest(BaseModel):
    key_name: str
    secret_value: str
    org_id: str


class TogglePlatformToolRequest(BaseModel):
    enabled: bool


# ─── Agent secrets ────────────────────────────────────────────────────────────

@router.get("/{agent_id}/secrets")
def list_agent_secrets(agent_id: str):
    res = (
        supabase.from_("agent_secrets")
        .select("id, key_name, created_at")
        .eq("agent_id", agent_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


@router.post("/{agent_id}/secrets")
def add_agent_secret(agent_id: str, body: AddSecretRequest):
    res = supabase.rpc("insert_agent_secret", {
        "p_agent_id": agent_id,
        "p_key_name": body.key_name,
        "p_secret":   body.secret_value,
    }).execute()
    if res.data is None:
        raise HTTPException(status_code=500, detail="Failed to store secret.")
    secret_row = (
        supabase.from_("agent_secrets")
        .select("id, key_name, created_at")
        .eq("agent_id", agent_id)
        .eq("key_name", body.key_name)
        .single()
        .execute()
    )
    return secret_row.data


@router.delete("/{agent_id}/secrets/{key_name}")
def delete_agent_secret(agent_id: str, key_name: str):
    supabase.rpc("delete_agent_secret", {
        "p_agent_id": agent_id,
        "p_key_name": key_name,
    }).execute()
    return {"ok": True}


# ─── Platform tools ───────────────────────────────────────────────────────────

@router.get("/platform-tools")
def list_platform_tools():
    res = (
        supabase.from_("platform_tools")
        .select("*")
        .eq("is_active", True)
        .execute()
    )
    return res.data or []


@router.get("/{agent_id}/platform-tools")
def get_agent_platform_tools(agent_id: str):
    res = (
        supabase.from_("agent_platform_tools")
        .select("platform_tool_key")
        .eq("agent_id", agent_id)
        .execute()
    )
    return [row["platform_tool_key"] for row in (res.data or [])]


@router.put("/{agent_id}/platform-tools/{tool_key}")
def toggle_platform_tool(agent_id: str, tool_key: str, body: TogglePlatformToolRequest):
    if body.enabled:
        supabase.from_("agent_platform_tools").upsert({
            "agent_id":          agent_id,
            "platform_tool_key": tool_key,
        }).execute()
    else:
        supabase.from_("agent_platform_tools").delete().eq("agent_id", agent_id).eq("platform_tool_key", tool_key).execute()
    return {"ok": True, "enabled": body.enabled}