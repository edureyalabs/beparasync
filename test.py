# beparasync/test_app_pipeline.py
"""
Run from beparasync root:  python test_app_pipeline.py

Tests:
  1. What files currently exist in Storage for the known task
  2. What task_workspace_files DB records exist
  3. Direct sandbox call — does output_files capture /workspace/ writes?
  4. Direct sandbox call — does output_files capture relative path writes?
  5. Full mini agent run that writes an app and checks Storage after push
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from db import supabase
from sandbox_client import run_in_sandbox
from workspace_manager import (
    pull_workspace, push_workspace, snapshot_hashes,
    _storage_prefix, BUCKET
)

# ── CONFIG ─────────────────────────────────────────────────────────────────
# Paste the IDs from the broken app URL:
# https://parasync.in/apps/{AGENT_ID}/{TASK_ID}/ai-news-feed
AGENT_ID = "7ccc100b-4340-4f5a-8d35-64484b34277b"
TASK_ID  = "d256ca41-df4f-4e07-95db-f26b7f70fb7f"
APP_NAME = "ai-news-feed"

SEP = "─" * 60


def hdr(title: str):
    print(f"\n{SEP}\n  {title}\n{SEP}")


# ── 1. STORAGE INVENTORY ───────────────────────────────────────────────────
def test_1_storage_inventory():
    hdr("TEST 1 · Storage inventory for this task")

    task_res = supabase.from_("tasks").select("org_id").eq("id", TASK_ID).single().execute()
    if not task_res.data:
        print("❌  Task not found in DB")
        return None
    org_id = task_res.data["org_id"]
    print(f"org_id  : {org_id}")

    prefix = _storage_prefix(org_id, AGENT_ID, TASK_ID)
    print(f"prefix  : {prefix}")

    try:
        items = supabase.storage.from_(BUCKET).list(prefix)
        print(f"Root objects: {[i['name'] for i in (items or [])]}")
    except Exception as e:
        print(f"Root list error: {e}")

    # Try to list the apps sub-folder
    apps_prefix = f"{prefix}/apps"
    try:
        items = supabase.storage.from_(BUCKET).list(apps_prefix)
        print(f"apps/ objects: {[i['name'] for i in (items or [])]}")
    except Exception as e:
        print(f"apps/ list error: {e}")

    # Try to list the specific app folder
    app_prefix = f"{prefix}/apps/{APP_NAME}"
    try:
        items = supabase.storage.from_(BUCKET).list(app_prefix)
        print(f"apps/{APP_NAME}/ objects: {[i['name'] for i in (items or [])]}")
    except Exception as e:
        print(f"apps/{APP_NAME}/ list error: {e}")

    # Try direct download of index.html
    key = f"{app_prefix}/index.html"
    try:
        content = supabase.storage.from_(BUCKET).download(key)
        print(f"✅  index.html EXISTS ({len(content)} bytes)")
        print(f"    Preview: {content[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        print(f"❌  index.html NOT found: {e}")

    return org_id


# ── 2. DB RECORDS ──────────────────────────────────────────────────────────
def test_2_db_records():
    hdr("TEST 2 · task_workspace_files DB records")

    rows = (
        supabase.from_("task_workspace_files")
        .select("file_path, size_bytes, written_by, updated_at")
        .eq("task_id", TASK_ID)
        .order("file_path")
        .execute()
    )
    if not rows.data:
        print("❌  No rows in task_workspace_files for this task")
    else:
        print(f"✅  {len(rows.data)} file record(s):")
        for r in rows.data:
            print(f"    {r['file_path']}  ({r['size_bytes']} bytes)  by={r['written_by']}")


# ── 3. SANDBOX — absolute /workspace/ path (current agent behaviour) ───────
async def test_3_sandbox_absolute_path():
    hdr("TEST 3 · Sandbox with ABSOLUTE /workspace/ path (current agent behaviour)")

    code = """
import os, pathlib, json

app_dir = pathlib.Path('/workspace/apps/test-absolute')
app_dir.mkdir(parents=True, exist_ok=True)
(app_dir / 'index.html').write_text('<h1>Absolute path test</h1>')
(app_dir / 'data.json').write_text(json.dumps({'source': 'absolute'}))

print('Written to:', str(app_dir))
print('Files:', os.listdir(app_dir))
"""

    result = await run_in_sandbox(
        code=code,
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": AGENT_ID},
        packages=[],
        agent_id=AGENT_ID,
        timeout=30,
    )

    print(f"exit_code   : {result['exit_code']}")
    print(f"stdout      : {result['stdout']}")
    print(f"stderr      : {result['stderr']}")
    print(f"output_files: {list(result.get('output_files', {}).keys())}")

    if result.get('output_files'):
        print("✅  output_files captured — absolute path WORKS")
    else:
        print("❌  output_files EMPTY — absolute path is NOT captured by sdparasync snapshot")


# ── 4. SANDBOX — relative path (proposed fix) ──────────────────────────────
async def test_4_sandbox_relative_path():
    hdr("TEST 4 · Sandbox with RELATIVE path (proposed fix)")

    code = """
import os, pathlib, json

app_dir = pathlib.Path('apps/test-relative')
app_dir.mkdir(parents=True, exist_ok=True)
(app_dir / 'index.html').write_text('<h1>Relative path test</h1>')
(app_dir / 'data.json').write_text(json.dumps({'source': 'relative'}))

print('Written to:', str(app_dir))
print('Files:', os.listdir(app_dir))
"""

    result = await run_in_sandbox(
        code=code,
        entry_point="main.py",
        files={},
        env_vars={"__AGENT_ID__": AGENT_ID},
        packages=[],
        agent_id=AGENT_ID,
        timeout=30,
    )

    print(f"exit_code   : {result['exit_code']}")
    print(f"stdout      : {result['stdout']}")
    print(f"stderr      : {result['stderr']}")
    print(f"output_files: {list(result.get('output_files', {}).keys())}")

    if result.get('output_files'):
        print("✅  output_files captured — relative path WORKS")
    else:
        print("❌  output_files EMPTY — relative path also broken (unexpected)")


# ── 5. FULL END-TO-END: sandbox → push → storage verify ───────────────────
async def test_5_full_pipeline(org_id: str):
    hdr("TEST 5 · Full pipeline: sandbox write → push_workspace → storage verify")

    TEST_APP = "pipeline-test"
    workspace_dir = Path(tempfile.mkdtemp())
    print(f"Local workspace_dir: {workspace_dir}")

    # Pull existing workspace
    pull_workspace(org_id, AGENT_ID, TASK_ID, workspace_dir)
    pre_hashes = snapshot_hashes(workspace_dir)
    print(f"Files pulled from storage: {list(pre_hashes.keys()) or '(none)'}")

    # Simulate what the agent does — relative path write
    code_relative = """
import os, pathlib, json

app_dir = pathlib.Path('apps/pipeline-test')
app_dir.mkdir(parents=True, exist_ok=True)
(app_dir / 'index.html').write_text('<!DOCTYPE html><html><body><h1>Pipeline Test App</h1></body></html>')
(app_dir / 'update.py').write_text('import json\\nprint(json.dumps({"status": "ok"}))')
(app_dir / 'data.json').write_text(json.dumps({"test": True}))

print('Files:', os.listdir(app_dir))
"""

    # Build file payload from workspace (same as handle_sandbox_tool_call)
    workspace_files: dict[str, bytes] = {}
    if workspace_dir.exists():
        for f in workspace_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(workspace_dir)).replace("\\", "/")
                workspace_files[rel] = f.read_bytes()

    print(f"Sending {len(workspace_files)} files to sandbox")

    result = await run_in_sandbox(
        code=code_relative,
        entry_point="main.py",
        files={n: c for n, c in workspace_files.items()},
        env_vars={"__AGENT_ID__": AGENT_ID},
        packages=[],
        agent_id=AGENT_ID,
        timeout=30,
    )

    print(f"exit_code   : {result['exit_code']}")
    print(f"stdout      : {result['stdout']}")
    print(f"stderr      : {result['stderr'][:300] if result.get('stderr') else ''}")
    print(f"output_files returned: {list(result.get('output_files', {}).keys())}")

    # Write output_files into workspace_dir (same as handle_sandbox_tool_call)
    for filename, b64 in result.get("output_files", {}).items():
        dest = workspace_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(b64))
        print(f"  Wrote to workspace_dir: {filename}")

    # What does workspace_dir look like now?
    after_files = list(workspace_dir.rglob("*"))
    print(f"\nworkspace_dir contents after sandbox ({len(after_files)} items):")
    for f in after_files:
        if f.is_file():
            print(f"  {f.relative_to(workspace_dir)}")

    # Push to storage
    print("\nPushing workspace to storage...")
    push_workspace(org_id, AGENT_ID, TASK_ID, workspace_dir, pre_hashes)
    print("Push complete.")

    # Verify in storage
    app_key = f"{_storage_prefix(org_id, AGENT_ID, TASK_ID)}/apps/{TEST_APP}/index.html"
    try:
        content = supabase.storage.from_(BUCKET).download(app_key)
        print(f"\n✅  STORAGE VERIFY: index.html found ({len(content)} bytes)")
        print(f"    URL would be: /apps/{AGENT_ID}/{TASK_ID}/{TEST_APP}")
    except Exception as e:
        print(f"\n❌  STORAGE VERIFY: index.html NOT in storage: {e}")

    # Cleanup local temp
    import shutil
    shutil.rmtree(workspace_dir, ignore_errors=True)


# ── 6. CHECK WHAT THE APP ROUTE WOULD RETURN ──────────────────────────────
def test_6_app_route_simulation(org_id: str):
    hdr("TEST 6 · Simulate what GET /app/{agentId}/{taskId}/{appName} does")

    key = f"{_storage_prefix(org_id, AGENT_ID, TASK_ID)}/apps/{APP_NAME}/index.html"
    print(f"Looking up storage key: {key}")
    try:
        content = supabase.storage.from_(BUCKET).download(key)
        print(f"✅  Found! ({len(content)} bytes) — app SHOULD serve correctly")
    except Exception as e:
        print(f"❌  Not found: {e}")
        print("    This is why the frontend shows 'App not found (404)'")
        print("    The file was never pushed to this storage path.")


# ── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    print("\n🔬  PARASYNC APP PIPELINE DIAGNOSTIC")
    print(f"    Agent : {AGENT_ID}")
    print(f"    Task  : {TASK_ID}")
    print(f"    App   : {APP_NAME}")

    org_id = test_1_storage_inventory()
    test_2_db_records()
    await test_3_sandbox_absolute_path()
    await test_4_sandbox_relative_path()

    if org_id:
        await test_5_full_pipeline(org_id)
        test_6_app_route_simulation(org_id)
    else:
        print("\n⚠️  Skipping tests 5 & 6 — could not resolve org_id")

    print(f"\n{SEP}\n  DIAGNOSTIC COMPLETE\n{SEP}\n")


if __name__ == "__main__":
    asyncio.run(main())