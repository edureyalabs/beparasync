# beparasync/routes/files.py

import os
import re
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from db import supabase
from workspace_manager import _storage_prefix, BUCKET

router = APIRouter(prefix="/files", tags=["files"])


def _safe_filename(filename: str) -> str:
    clean = re.sub(r"\.\./|\.\.\\", "", filename).strip("/").strip("\\")
    if not clean or "/" in clean:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return clean


def _user_storage_path(org_id: str, agent_id: str, task_id: str, filename: str) -> str:
    """Path for user-uploaded files (task_files table)."""
    return f"{org_id}/{agent_id}/{task_id}/{filename}"


def _task_meta(task_id: str) -> dict:
    res = supabase.from_("tasks").select("id, agent_id, org_id").eq("id", task_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Task not found.")
    return res.data


# ── GET /files/tasks/{task_id} ── unified file list ──────────────────────────
# Returns both user-uploaded files (task_files) and agent workspace files
# (task_workspace_files) in a single merged list the frontend can render.

@router.get("/tasks/{task_id}")
def list_files(task_id: str):
    task = _task_meta(task_id)

    # User-uploaded files
    user_res = (
        supabase.from_("task_files")
        .select("id, filename, storage_path, size_bytes, mime_type, uploaded_by, created_at")
        .eq("task_id", task_id)
        .order("created_at", desc=False)
        .execute()
    )
    user_files = [
        {**row, "source": "upload", "file_path": row["filename"]}
        for row in (user_res.data or [])
    ]

    # Agent workspace files
    ws_res = (
        supabase.from_("task_workspace_files")
        .select("id, file_path, size_bytes, written_by, updated_at")
        .eq("task_id", task_id)
        .order("file_path", desc=False)
        .execute()
    )
    ws_files = [
        {
            **row,
            "source":       "workspace",
            "filename":     row["file_path"].split("/")[-1],
            "storage_path": f"{_storage_prefix(task['org_id'], task['agent_id'], task_id)}/{row['file_path']}",
            "mime_type":    None,
            "uploaded_by":  row["written_by"],
            "created_at":   row["updated_at"],
        }
        for row in (ws_res.data or [])
    ]

    return {"user_files": user_files, "workspace_files": ws_files}


# ── POST /files/tasks/{task_id}/upload ───────────────────────────────────────

@router.post("/tasks/{task_id}/upload")
async def upload_file(task_id: str, file: UploadFile = File(...)):
    task     = _task_meta(task_id)
    filename = _safe_filename(file.filename or "upload")
    storage_path = _user_storage_path(task["org_id"], task["agent_id"], task_id, filename)

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
        "filename":     filename,
        "storage_path": storage_path,
        "size_bytes":   len(content),
        "mime_type":    file.content_type,
        "uploaded_by":  "user",
    }).select().single().execute()

    return row.data


# ── GET /files/tasks/{task_id}/download ── signed URL by storage_path ────────
# Works for both user files and workspace files since both have storage_path.

@router.get("/tasks/{task_id}/download")
def download_file(task_id: str, storage_path: str):
    """
    Returns a signed URL for any file in the task, identified by its full
    storage_path. The frontend passes storage_path from the unified list.
    """
    _task_meta(task_id)  # auth check — task must exist

    try:
        signed = supabase.storage.from_(BUCKET).create_signed_url(storage_path, expires_in=300)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {e}")

    if not signed or not signed.get("signedURL"):
        raise HTTPException(status_code=404, detail="File not found.")

    return {"url": signed["signedURL"], "expires_in": 300}


# ── DELETE /files/tasks/{task_id}/user/{filename} ── user upload only ─────────

@router.delete("/tasks/{task_id}/user/{filename}")
def delete_user_file(task_id: str, filename: str):
    task     = _task_meta(task_id)
    filename = _safe_filename(filename)
    storage_path = _user_storage_path(task["org_id"], task["agent_id"], task_id, filename)

    supabase.storage.from_(BUCKET).remove([storage_path])
    supabase.from_("task_files").delete().eq("task_id", task_id).eq("filename", filename).execute()

    return {"ok": True}


# ── DELETE /files/tasks/{task_id}/workspace ── agent workspace file ───────────

@router.delete("/tasks/{task_id}/workspace")
def delete_workspace_file(task_id: str, file_path: str):
    """
    Delete a workspace file by its file_path (e.g. apps/my-app/index.html).
    """
    task = _task_meta(task_id)
    storage_path = f"{_storage_prefix(task['org_id'], task['agent_id'], task_id)}/{file_path}"

    supabase.storage.from_(BUCKET).remove([storage_path])
    supabase.from_("task_workspace_files").delete().eq("task_id", task_id).eq("file_path", file_path).execute()

    return {"ok": True}


# ── GET /files/tasks/{task_id}/workspace/download ── signed URL for workspace file

@router.get("/tasks/{task_id}/workspace/download")
def download_workspace_file(task_id: str, file_path: str):
    task = _task_meta(task_id)
    storage_path = f"{_storage_prefix(task['org_id'], task['agent_id'], task_id)}/{file_path}"

    try:
        signed = supabase.storage.from_(BUCKET).create_signed_url(storage_path, expires_in=300)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {e}")

    if not signed or not signed.get("signedURL"):
        raise HTTPException(status_code=404, detail="File not found.")

    return {"url": signed["signedURL"], "expires_in": 300}