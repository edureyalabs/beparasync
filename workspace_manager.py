# beparasync/workspace_manager.py

import base64
import hashlib
import os
from pathlib import Path

from db import supabase

BUCKET = "agent-files"


def _storage_prefix(org_id: str, agent_id: str, task_id: str) -> str:
    return f"{org_id}/{agent_id}/tasks/{task_id}"


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def pull_workspace(org_id: str, agent_id: str, task_id: str, local_dir: Path):
    """Pull all task workspace files from Supabase Storage into local_dir."""
    local_dir.mkdir(parents=True, exist_ok=True)

    res = (
        supabase.from_("task_workspace_files")
        .select("file_path")
        .eq("task_id", task_id)
        .execute()
    )

    prefix = _storage_prefix(org_id, agent_id, task_id)

    for row in (res.data or []):
        file_path  = row["file_path"]
        storage_key = f"{prefix}/{file_path}"
        try:
            content = supabase.storage.from_(BUCKET).download(storage_key)
            dest    = local_dir / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        except Exception:
            pass


def push_workspace(
    org_id: str, agent_id: str, task_id: str, local_dir: Path,
    pre_run_hashes: dict[str, str],
):
    """
    Diff-based push: only upload new/changed files, delete removed files.
    Updates task_workspace_files table to reflect current state.
    """
    prefix = _storage_prefix(org_id, agent_id, task_id)

    current_files: dict[str, bytes] = {}
    if local_dir.exists():
        for f in local_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(local_dir))
                current_files[rel] = f.read_bytes()

    # Upload new/changed files
    for rel_path, content in current_files.items():
        file_hash = _hash(content)
        if pre_run_hashes.get(rel_path) == file_hash:
            continue  # unchanged

        storage_key = f"{prefix}/{rel_path}"
        supabase.storage.from_(BUCKET).upload(
            path=storage_key,
            file=content,
            file_options={"upsert": "true"},
        )

        supabase.from_("task_workspace_files").upsert({
            "task_id":      task_id,
            "agent_id":     agent_id,
            "org_id":       org_id,
            "file_path":    rel_path,
            "size_bytes":   len(content),
            "content_hash": file_hash,
            "written_by":   "agent",
            "updated_at":   "now()",
        }, on_conflict="task_id,file_path").execute()

    # Delete files the agent removed
    for old_path in set(pre_run_hashes.keys()) - set(current_files.keys()):
        storage_key = f"{prefix}/{old_path}"
        try:
            supabase.storage.from_(BUCKET).remove([storage_key])
        except Exception:
            pass
        supabase.from_("task_workspace_files").delete().eq("task_id", task_id).eq("file_path", old_path).execute()


def snapshot_hashes(local_dir: Path) -> dict[str, str]:
    """Record sha256 of all files before a run for diff comparison after."""
    hashes = {}
    if local_dir.exists():
        for f in local_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(local_dir))
                hashes[rel] = _hash(f.read_bytes())
    return hashes


def get_task_md(org_id: str, agent_id: str, task_id: str) -> str:
    """Fetch TASK.md content from storage if it exists."""
    key = f"{_storage_prefix(org_id, agent_id, task_id)}/TASK.md"
    try:
        content = supabase.storage.from_(BUCKET).download(key)
        return content.decode("utf-8", errors="replace")
    except Exception:
        return ""


def list_workspace_files(task_id: str) -> list[dict]:
    res = (
        supabase.from_("task_workspace_files")
        .select("file_path, size_bytes, written_by, updated_at")
        .eq("task_id", task_id)
        .order("file_path")
        .execute()
    )
    return res.data or []