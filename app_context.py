# beparasync/app_context.py
"""
Reads APPS.md from the agent's task workspace and injects it into the system prompt.
Also provides the APP_BUILDER_INSTRUCTIONS block that tells the agent how to build apps.
"""
from workspace_manager import BUCKET
from db import supabase


def get_apps_md(org_id: str, agent_id: str, task_id: str) -> str:
    """Fetch APPS.md from storage if it exists."""
    key = f"{org_id}/{agent_id}/tasks/{task_id}/APPS.md"
    try:
        content = supabase.storage.from_(BUCKET).download(key)
        return content.decode("utf-8", errors="replace")
    except Exception:
        return ""


APP_BUILDER_INSTRUCTIONS = (
    "\n\u2550\u2550 AGENT APP BUILDER \u2550\u2550\n"
    "You can build interactive web apps that run inside the Parasync platform.\n\n"

    "CRITICAL RULES - FOLLOW EXACTLY:\n"
    "1. ALL app files MUST use RELATIVE paths: ./apps/{app-name}/ — NEVER absolute paths like /workspace/apps/\n"
    "2. The sandbox cwd is your workspace root. Relative paths are the ONLY way files get saved.\n"
    "3. Every app needs exactly these 3 files:\n"
    "   - ./apps/{app-name}/index.html  -> full frontend (HTML+CSS+JS inline)\n"
    "   - ./apps/{app-name}/update.py   -> backend Python script\n"
    "   - ./apps/{app-name}/data.json   -> initial seed data\n\n"

    "HOW TO BUILD - STEP BY STEP:\n"
    "Step 1: Write index.html to ./apps/{app-name}/index.html\n"
    "Step 2: Write update.py to ./apps/{app-name}/update.py\n"
    "Step 3: Write data.json to ./apps/{app-name}/data.json\n"
    "Step 4: VERIFY files exist: print(os.listdir('./apps/{app-name}'))\n"
    "Step 5: Update ./APPS.md with the app entry\n"
    "Step 6: Tell user the exact full URL (provided below)\n\n"

    "EXAMPLE PYTHON CODE to write files correctly:\n"
    "    import os, json, pathlib\n"
    "    app_dir = pathlib.Path('./apps/my-app')   # <-- RELATIVE, not /workspace/apps/my-app\n"
    "    app_dir.mkdir(parents=True, exist_ok=True)\n"
    "    (app_dir / 'index.html').write_text('<!DOCTYPE html><html>...</html>')\n"
    "    (app_dir / 'update.py').write_text('import json\\nprint(json.dumps({\"status\": \"updated\"}))')\n"
    "    (app_dir / 'data.json').write_text(json.dumps([{'key': 'value'}]))\n"
    "    print('Files:', os.listdir(app_dir))  # ALWAYS verify — if empty, something went wrong\n\n"

    "ABSOLUTE PATHS WILL SILENTLY FAIL — files written to /workspace/... are NOT saved.\n"
    "ALWAYS use pathlib.Path('./apps/my-app') not pathlib.Path('/workspace/apps/my-app')\n\n"

    "FRONTEND SDK - window.__APP__ is injected automatically:\n"
    "    await __APP__.data('data.json')              -> read data file\n"
    "    await __APP__.setData('data.json', value)    -> write data file\n"
    "    await __APP__.run('update.py', {params})     -> run Python script\n\n"

    "PYTHON SCRIPT RULES:\n"
    "- Must print a JSON object to stdout: print(json.dumps({...}))\n"
    "- Read/write data files using relative paths e.g. open('data.json')\n"
    "- Agent secrets available via os.environ['KEY_NAME']\n\n"

    "ALWAYS verify files were written by listing the directory:\n"
    "    import os\n"
    "    print(os.listdir('./apps/my-app'))  # must show index.html, update.py, data.json\n"
)


def build_app_context_block(apps_md: str) -> str:
    if not apps_md:
        return APP_BUILDER_INSTRUCTIONS + "\nNo apps built yet for this task."
    return APP_BUILDER_INSTRUCTIONS + f"\n\n\u2550\u2550 EXISTING APPS \u2550\u2550\n{apps_md}"