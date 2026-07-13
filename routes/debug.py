# beparasync/routes/debug.py
import os
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from db import supabase
from workspace_manager import _storage_prefix, BUCKET

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/apps-list")
async def debug_apps_list(
    agent_id: str,
    task_id: str,
    authorization: str | None = Header(default=None),
):
    task_res = supabase.from_("tasks").select("org_id").eq("id", task_id).single().execute()
    if not task_res.data:
        return {"error": "task not found"}
    org_id = task_res.data["org_id"]

    prefix      = _storage_prefix(org_id, agent_id, task_id)
    apps_prefix = f"{prefix}/apps"

    try:
        root_items = supabase.storage.from_(BUCKET).list(prefix)
        root_names = [i["name"] for i in (root_items or [])]
    except Exception as e:
        root_names = [f"ERROR: {e}"]

    try:
        apps_items = supabase.storage.from_(BUCKET).list(apps_prefix)
        apps_names = [i["name"] for i in (apps_items or [])]
    except Exception as e:
        apps_names = [f"ERROR: {e}"]

    app_details = {}
    for app_name in apps_names:
        if str(app_name).startswith("ERROR"):
            continue
        try:
            files = supabase.storage.from_(BUCKET).list(f"{apps_prefix}/{app_name}")
            app_details[app_name] = [f["name"] for f in (files or [])]
        except Exception as e:
            app_details[app_name] = [f"ERROR: {e}"]

    index_check = {}
    for app_name in list(app_details.keys()):
        key = f"{apps_prefix}/{app_name}/index.html"
        try:
            content = supabase.storage.from_(BUCKET).download(key)
            index_check[app_name] = f"EXISTS ({len(content)} bytes)"
        except Exception as e:
            index_check[app_name] = f"NOT FOUND: {e}"

    # Validate JWT
    token_info = {}
    user_id = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            user_res = supabase.auth.get_user(token)
            if user_res and user_res.user:
                user_id = user_res.user.id
                token_info = {"valid": True, "user_id": user_id, "email": user_res.user.email}
            else:
                token_info = {"valid": False, "reason": "no user returned"}
        except Exception as e:
            token_info = {"valid": False, "reason": str(e)}
    else:
        token_info = {"valid": False, "reason": "no Authorization header provided"}

    # Check org ownership
    org_access = None
    if user_id:
        try:
            org_res = (
                supabase.from_("organizations")
                .select("id")
                .eq("id", org_id)
                .eq("owner_id", user_id)
                .execute()
            )
            org_access = "✅ user owns this org" if org_res.data else "❌ user does NOT own this org — _apps will 403"
        except Exception as e:
            org_access = f"ERROR checking org: {e}"

    # Simulate _apps route
    try:
        sim_items = supabase.storage.from_(BUCKET).list(apps_prefix)
        simulated_apps_route = {"apps": [i["name"] for i in (sim_items or []) if i.get("name")]}
    except Exception as e:
        simulated_apps_route = {"apps": [], "error": str(e)}

    # Check CORS env var
    allowed_origins = os.getenv("ALLOWED_ORIGINS", "NOT SET")

    # Check workspace DB records
    try:
        ws_rows = (
            supabase.from_("task_workspace_files")
            .select("file_path, size_bytes, updated_at")
            .eq("task_id", task_id)
            .ilike("file_path", "apps/%")
            .order("file_path")
            .execute()
        )
        workspace_db_records = ws_rows.data or []
    except Exception as e:
        workspace_db_records = [{"error": str(e)}]

    return {
        "org_id":                org_id,
        "storage_prefix":        prefix,
        "apps_prefix":           apps_prefix,
        "bucket":                BUCKET,
        "allowed_origins":       allowed_origins,
        "root_items":            root_names,
        "apps_folder_items":     apps_names,
        "app_file_details":      app_details,
        "index_html_check":      index_check,
        "workspace_db_records":  workspace_db_records,
        "token_info":            token_info,
        "org_access":            org_access,
        "simulated_apps_route":  simulated_apps_route,
        "verdict": (
            "✅ Storage has apps — check CORS (allowed_origins) and token auth in frontend"
            if simulated_apps_route.get("apps")
            else "❌ Storage listing empty or failing"
        ),
    }


@router.get("/cors-check")
async def cors_check():
    """Hit this from the browser console on parasync.in to verify CORS is working."""
    return {
        "ok": True,
        "allowed_origins": os.getenv("ALLOWED_ORIGINS", "NOT SET"),
        "message": "If you can read this from the frontend, CORS is fine.",
    }