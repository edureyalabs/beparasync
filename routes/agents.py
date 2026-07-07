# routes/agents.py
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from db import supabase
from agent_runner import run_agent_task

router = APIRouter(prefix="/agents", tags=["agents"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class TriggerRunRequest(BaseModel):
    org_id: str


# ─── POST /agents/tasks/{task_id}/run ─────────────────────────────────────────
# Trigger a manual run. All task/agent CRUD goes directly to Supabase from frontend.

@router.post("/tasks/{task_id}/run")
async def trigger_run(task_id: str, body: TriggerRunRequest, background_tasks: BackgroundTasks):
    task_res = supabase.from_("tasks").select("*").eq("id", task_id).single().execute()
    if not task_res.data:
        raise HTTPException(status_code=404, detail="Task not found.")
    task = task_res.data

    # Insert run record
    supabase.from_("runs").insert({
        "task_id":      task_id,
        "agent_id":     task["agent_id"],
        "org_id":       body.org_id,
        "status":       "queued",
        "triggered_by": "manual",
    }).execute()

    # Fetch the run we just created
    run_res = (
        supabase.from_("runs")
        .select("*")
        .eq("task_id", task_id)
        .eq("status", "queued")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not run_res.data:
        raise HTTPException(status_code=500, detail="Failed to create run.")
    run = run_res.data[0]

    # Fire agentic loop in background — frontend polls for status
    background_tasks.add_task(run_agent_task, run["id"], task_id, task["agent_id"])

    return {"run_id": run["id"], "status": "queued"}


# ─── POST /agents/runs/{run_id}/cancel ────────────────────────────────────────
# Sets status to 'cancelled' — the agentic loop checks this between iterations
# and exits cleanly. FastAPI background task finishes naturally, no hard kill.

@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run_res = supabase.from_("runs").select("status").eq("id", run_id).single().execute()
    if not run_res.data:
        raise HTTPException(status_code=404, detail="Run not found.")

    status = run_res.data["status"]
    if status not in ("queued", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Run cannot be cancelled — current status is '{status}'."
        )

    supabase.from_("runs").update({
        "status": "cancelled",
    }).eq("id", run_id).execute()

    return {"ok": True, "run_id": run_id, "status": "cancelled"}


# ─── GET /agents/runs/{run_id} ────────────────────────────────────────────────
# Frontend polls this every 2s to get live status + steps.

@router.get("/runs/{run_id}")
def get_run(run_id: str):
    res = supabase.from_("runs").select("*").eq("id", run_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Run not found.")
    return res.data