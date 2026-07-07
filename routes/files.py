# beparasync/routes/files.py

import os
import re
from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from pydantic import BaseModel

from db import supabase

router = APIRouter(prefix="/files", tags=["files"])

BUCKET = "agent-files"


def _safe_storage_path(org_id: str, agent_id: str, task_id: str, filename: str) -> str:
    clean = re.sub(r"\.\./|\.\.\\", "", filename).strip("/").strip("\\")
    if not clean or "/" in clean:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return f"{org_id}/{agent_id}/{task_id}/{clean}"


def _task_meta(task_id: str) -> dict:
    res = supabase.from_("tasks").select("id, agent_id, org_id").eq("id", task_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Task not found.")
    return res.data


# ─── GET /files/tasks/{task_id} ───────────────────────────────────────────────

@router.get("/tasks/{task_id}")
def list_files(task_id: str):
    res = (
        supabase.from_("task_files")
        .select("*")
        .eq("task_id", task_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


# ─── POST /files/tasks/{task_id}/upload ───────────────────────────────────────

@router.post("/tasks/{task_id}/upload")
async def upload_file(task_id: str, file: UploadFile = File(...)):
    task = _task_meta(task_id)
    storage_path = _safe_storage_path(task["org_id"], task["agent_id"], task_id, file.filename or "upload")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 50MB limit.")

    supabase.storage.from_(BUCKET).upload(
        path=storage_path,
        file=content,
        file_options={"content-type": file.content_type or "application/octet-stream", "upsert": "true"},
    )

    row = supabase.from_("task_files").insert({
        "task_id":      task_id,
        "agent_id":     task["agent_id"],
        "org_id":       task["org_id"],
        "filename":     file.filename,
        "storage_path": storage_path,
        "size_bytes":   len(content),
        "mime_type":    file.content_type,
        "uploaded_by":  "user",
    }).select().single().execute()

    return row.data


# ─── GET /files/tasks/{task_id}/{filename} ────────────────────────────────────

@router.get("/tasks/{task_id}/{filename}")
def download_file(task_id: str, filename: str):
    task = _task_meta(task_id)
    storage_path = _safe_storage_path(task["org_id"], task["agent_id"], task_id, filename)

    signed = supabase.storage.from_(BUCKET).create_signed_url(storage_path, expires_in=300)
    if not signed or not signed.get("signedURL"):
        raise HTTPException(status_code=404, detail="File not found.")

    return {"url": signed["signedURL"], "expires_in": 300}


# ─── DELETE /files/tasks/{task_id}/{filename} ─────────────────────────────────

@router.delete("/tasks/{task_id}/{filename}")
def delete_file(task_id: str, filename: str):
    task = _task_meta(task_id)
    storage_path = _safe_storage_path(task["org_id"], task["agent_id"], task_id, filename)

    supabase.storage.from_(BUCKET).remove([storage_path])

    supabase.from_("task_files").delete().eq("task_id", task_id).eq("filename", filename).execute()

    return {"ok": True}