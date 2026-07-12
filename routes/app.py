# beparasync/routes/app.py
import json
import os
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Any

from db import supabase
from sandbox_client import run_in_sandbox
from workspace_manager import _storage_prefix, BUCKET

router = APIRouter(prefix="/app", tags=["app"])

BACKEND = os.getenv("BACKEND_URL", "")  # e.g. https://beparasync.up.railway.app


def _verify(authorization: str | None) -> str:
    """Validate Supabase JWT and return user_id."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    try:
        # Validate token against Supabase — this calls the Supabase auth API
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user_res.user.id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token validation failed: {e}")


def _app_prefix(org_id: str, agent_id: str, task_id: str, app_name: str) -> str:
    return f"{_storage_prefix(org_id, agent_id, task_id)}/apps/{app_name}"


def _resolve_task(agent_id: str, task_id: str) -> dict:
    res = supabase.from_("tasks").select("*").eq("id", task_id).eq("agent_id", agent_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Task not found")
    return res.data


def _check_org_access(user_id: str, org_id: str):
    """Ensure the authenticated user belongs to this org."""
    res = (
        supabase.from_("organizations")
        .select("id")
        .eq("id", org_id)
        .eq("owner_id", user_id)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=403, detail="Access denied")


# ── GET /app/{agent_id}/{task_id}/{app_name} → serve HTML ────────────────────
@router.get("/{agent_id}/{task_id}/{app_name}", response_class=HTMLResponse)
async def serve_app(
    agent_id: str, task_id: str, app_name: str,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    key = f"{_app_prefix(org_id, agent_id, task_id, app_name)}/index.html"
    try:
        content = supabase.storage.from_(BUCKET).download(key)
        html    = content.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found: {e}")

    # Use absolute backend URL so iframe fetch calls work cross-origin
    api_origin = BACKEND or ""

    sdk = f"""<script>
window.__APP__ = {{
  agentId: "{agent_id}", taskId: "{task_id}", appName: "{app_name}",
  _token: "",
  _h() {{
    return {{
      "Content-Type": "application/json",
      "Authorization": "Bearer " + (this._token || localStorage.getItem("sb-access-token") || "")
    }};
  }},
  async data(key) {{
    const r = await fetch("{api_origin}/app/{agent_id}/{task_id}/{app_name}/data/" + key, {{ headers: this._h() }});
    if (!r.ok) return null;
    return r.json();
  }},
  async setData(key, value) {{
    await fetch("{api_origin}/app/{agent_id}/{task_id}/{app_name}/data/" + key, {{
      method: "POST", headers: this._h(), body: JSON.stringify(value)
    }});
  }},
  async run(script, params={{}}) {{
    const r = await fetch("{api_origin}/app/{agent_id}/{task_id}/{app_name}/run/" + script, {{
      method: "POST", headers: this._h(), body: JSON.stringify({{ params }})
    }});
    return r.json();
  }},
  async listData() {{
    const r = await fetch("{api_origin}/app/{agent_id}/{task_id}/{app_name}/files", {{ headers: this._h() }});
    return r.ok ? r.json() : [];
  }}
}};
// Allow parent window to inject the auth token
window.addEventListener("message", (e) => {{
  if (e.data && e.data.type === "PARASYNC_TOKEN") {{
    window.__APP__._token = e.data.token;
  }}
}});
</script>"""

    html = html.replace("</head>", sdk + "\n</head>") if "</head>" in html else sdk + html
    return HTMLResponse(content=html, headers={
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "frame-ancestors 'self'",
    })


# ── GET /app/{agent_id}/{task_id}/{app_name}/data/{key} → read ───────────────
@router.get("/{agent_id}/{task_id}/{app_name}/data/{key}")
async def read_data(
    agent_id: str, task_id: str, app_name: str, key: str,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    storage_key = f"{_app_prefix(org_id, agent_id, task_id, app_name)}/{key}"
    try:
        raw = supabase.storage.from_(BUCKET).download(storage_key)
        try:
            return JSONResponse(content=json.loads(raw))
        except Exception:
            return JSONResponse(content={"raw": raw.decode("utf-8", errors="replace")})
    except Exception:
        raise HTTPException(status_code=404, detail=f"'{key}' not found")


# ── POST /app/{agent_id}/{task_id}/{app_name}/data/{key} → write ─────────────
@router.post("/{agent_id}/{task_id}/{app_name}/data/{key}")
async def write_data(
    agent_id: str, task_id: str, app_name: str, key: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    body    = await request.json()
    content = json.dumps(body, indent=2).encode("utf-8")
    skey    = f"{_app_prefix(org_id, agent_id, task_id, app_name)}/{key}"
    supabase.storage.from_(BUCKET).upload(
        path=skey, file=content,
        file_options={"upsert": "true", "content-type": "application/json"},
    )
    return {"ok": True}


# ── POST /app/{agent_id}/{task_id}/{app_name}/run/{script} → execute ──────────
class RunPayload(BaseModel):
    params: dict[str, Any] = {}


@router.post("/{agent_id}/{task_id}/{app_name}/run/{script}")
async def run_script(
    agent_id: str, task_id: str, app_name: str, script: str,
    body: RunPayload,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    app_prefix = _app_prefix(org_id, agent_id, task_id, app_name)

    try:
        script_content = supabase.storage.from_(BUCKET).download(f"{app_prefix}/{script}").decode("utf-8")
    except Exception:
        raise HTTPException(status_code=404, detail=f"Script '{script}' not found")

    # Download all JSON data files into sandbox workspace
    files: dict[str, bytes] = {}
    try:
        listed = supabase.storage.from_(BUCKET).list(app_prefix)
        for item in (listed or []):
            fname = item.get("name", "")
            if fname and fname.endswith(".json"):
                try:
                    files[fname] = supabase.storage.from_(BUCKET).download(f"{app_prefix}/{fname}")
                except Exception:
                    pass
    except Exception:
        pass

    from executor import fetch_agent_secrets
    agent_secrets = await fetch_agent_secrets(agent_id)

    env_vars = {
        "__APP_PARAMS__": json.dumps(body.params),
        "__APP_NAME__":   app_name,
        "__AGENT_ID__":   agent_id,
        "__TASK_ID__":    task_id,
        "__ORG_ID__":     org_id,
        **agent_secrets,
    }

    result = await run_in_sandbox(
        code=script_content,
        entry_point=script,
        files=files,
        env_vars=env_vars,
        packages=[],
        agent_id=agent_id,
        timeout=60,
    )

    import base64
    for filename, b64 in result.get("output_files", {}).items():
        fkey = f"{app_prefix}/{filename}"
        supabase.storage.from_(BUCKET).upload(
            path=fkey,
            file=base64.b64decode(b64),
            file_options={"upsert": "true"},
        )

    stdout = result.get("stdout", "").strip()
    try:
        output = json.loads(stdout)
    except Exception:
        output = {"output": stdout}

    return {
        "ok":        result.get("exit_code", -1) == 0,
        "output":    output,
        "stdout":    result.get("stdout", ""),
        "stderr":    result.get("stderr", ""),
        "exit_code": result.get("exit_code", -1),
    }


# ── GET /app/{agent_id}/{task_id}/{app_name}/files → list data files ──────────
@router.get("/{agent_id}/{task_id}/{app_name}/files")
async def list_files(
    agent_id: str, task_id: str, app_name: str,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    app_prefix = _app_prefix(org_id, agent_id, task_id, app_name)
    try:
        items = supabase.storage.from_(BUCKET).list(app_prefix)
        return [i["name"] for i in (items or []) if i.get("name")]
    except Exception:
        return []


# ── GET /app/{agent_id}/{task_id}/apps → list all apps ───────────────────────
@router.get("/{agent_id}/{task_id}/_apps")
async def list_apps(
    agent_id: str, task_id: str,
    authorization: str | None = Header(default=None),
):
    user_id = _verify(authorization)
    task    = _resolve_task(agent_id, task_id)
    org_id  = task["org_id"]
    _check_org_access(user_id, org_id)

    prefix = f"{_storage_prefix(org_id, agent_id, task_id)}/apps"
    try:
        items = supabase.storage.from_(BUCKET).list(prefix)
        return {"apps": [i["name"] for i in (items or []) if i.get("name")]}
    except Exception:
        return {"apps": []}