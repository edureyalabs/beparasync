# routes/tools.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any
import textwrap

from db import supabase
from executor import fetch_secrets, run_tool

router = APIRouter(prefix="/tools", tags=["tools"])


# ─── Request schema ───────────────────────────────────────────────────────────

class RunToolRequest(BaseModel):
    tool_id: str
    toolset_id: str
    code: str
    tool_name: str
    parameters: dict[str, Any]
    arguments: dict[str, Any]


# ─── Helpers ──────────────────────────────────────────────────────────────────

PY_TYPE_MAP = {
    "string": "str", "number": "float", "integer": "int",
    "boolean": "bool", "array": "list", "object": "dict",
}

def build_full_code(tool_name: str, parameters: dict[str, Any], body: str) -> str:
    """
    The editor only stores the function body — the def line is never persisted.
    Always reconstruct the full function from tool_name + parameters schema.
    """
    props = parameters.get("properties", {})
    args = [
        f"{name}: {PY_TYPE_MAP.get(meta.get('type', 'string'), 'str')}"
        for name, meta in props.items()
    ]
    signature = f"def {tool_name}({', '.join(args)}):"
    indented   = textwrap.indent(body.strip(), "    ")
    return f"{signature}\n{indented}"


def coerce_arguments(arguments: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """
    Test runner sends everything as strings from <input> fields.
    Coerce to the correct Python type based on the JSON Schema.
    """
    props = parameters.get("properties", {})
    coerced = {}
    for k, v in arguments.items():
        param_type = props.get(k, {}).get("type", "string")
        try:
            if param_type in ("number",):
                coerced[k] = float(v)
            elif param_type == "integer":
                coerced[k] = int(v)
            elif param_type == "boolean":
                coerced[k] = str(v).lower() in ("true", "1", "yes")
            else:
                coerced[k] = v
        except (ValueError, TypeError):
            coerced[k] = v
    return coerced


# ─── POST /tools/run ──────────────────────────────────────────────────────────

@router.post("/run")
async def run_tool_endpoint(body: RunToolRequest):
    if body.tool_id != "__preview__":
        # Saved tool — fetch code + parameters from DB
        tool_res = (
            supabase
            .from_("tools")
            .select("id, name, toolset_id, code, parameters")
            .eq("id", body.tool_id)
            .eq("toolset_id", body.toolset_id)
            .single()
            .execute()
        )
        if not tool_res.data:
            raise HTTPException(status_code=404, detail="Tool not found.")
        raw_body   = tool_res.data["code"]
        tool_name  = tool_res.data["name"]
        parameters = tool_res.data["parameters"]
    else:
        # Unsaved preview — use what the editor sent
        raw_body   = body.code
        tool_name  = body.tool_name
        parameters = body.parameters

    # Always build the full def from stored parameters — body only is stored/sent
    code = build_full_code(tool_name, parameters, raw_body)

    # Coerce argument types (test runner sends strings)
    coerced = coerce_arguments(body.arguments, parameters)

    # Fetch secrets from Supabase Vault (service role only)
    secrets = await fetch_secrets(body.toolset_id)

    # Execute in subprocess with secrets injected as env vars
    result = await run_tool(
        code=code,
        tool_name=tool_name,
        arguments=coerced,
        secrets=secrets,
    )

    if not result["ok"]:
        raise HTTPException(
            status_code=422,
            detail={
                "error": result.get("error"),
                "traceback": result.get("traceback"),
            },
        )

    return {"result": result["result"]}