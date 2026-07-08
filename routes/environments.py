# beparasync/routes/environments.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import supabase
from sandbox_client import install_in_sandbox

router = APIRouter(prefix="/environments", tags=["environments"])


class UpdatePackagesRequest(BaseModel):
    packages: list[str]
    org_id: str


def _get_or_create(agent_id: str, org_id: str) -> dict:
    res = supabase.from_("agent_environments").select("*").eq("agent_id", agent_id).execute()
    if res.data:
        return res.data[0]

    created = supabase.from_("agent_environments").insert({
        "agent_id": agent_id,
        "org_id":   org_id,
        "packages": [],
        "status":   "pending",
    }).select().single().execute()
    return created.data


# ─── GET /environments/agents/{agent_id} ──────────────────────────────────────

@router.get("/agents/{agent_id}")
def get_environment(agent_id: str, org_id: str):
    return _get_or_create(agent_id, org_id)


# ─── PATCH /environments/agents/{agent_id} ────────────────────────────────────
# Updates package list and triggers install in sandbox

@router.patch("/agents/{agent_id}")
async def update_packages(agent_id: str, body: UpdatePackagesRequest):
    env = _get_or_create(agent_id, body.org_id)

    cleaned = [p.strip() for p in body.packages if p.strip()]

    supabase.from_("agent_environments").update({
        "packages": cleaned,
        "status":   "installing",
    }).eq("agent_id", agent_id).execute()

    result = await install_in_sandbox(agent_id, cleaned)

    if result.get("ok"):
        import hashlib, json
        pkg_hash = hashlib.md5(json.dumps(sorted(cleaned)).encode()).hexdigest()
        supabase.from_("agent_environments").update({
            "status":        "ready",
            "packages_hash": pkg_hash,
            "last_built_at": "now()",
        }).eq("agent_id", agent_id).execute()
    else:
        supabase.from_("agent_environments").update({
            "status": "failed",
        }).eq("agent_id", agent_id).execute()

    return {
        "ok":      result.get("ok", False),
        "agent_id": agent_id,
        "packages": cleaned,
        "error":   result.get("error"),
    }