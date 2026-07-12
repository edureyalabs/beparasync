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


APP_BUILDER_INSTRUCTIONS = """
══ AGENT APP BUILDER ══
You can build interactive web apps that run inside the Parasync platform.

HOW TO BUILD AN APP:
1. Write files to /workspace/apps/{app-name}/
   - index.html   → the frontend (HTML + CSS + JS, all inline)
   - update.py    → backend script(s) the frontend can trigger
   - data.json    → initial data / database seed

2. The frontend has access to window.__APP__ SDK:
   - await __APP__.data("data.json")              → read a data file
   - await __APP__.setData("data.json", payload)  → write a data file  
   - await __APP__.run("update.py", {params})     → execute a Python script
     The script runs in sandbox, can read/write .json files, returns stdout as JSON.

3. After building, update /workspace/APPS.md:
   ## Apps
   - {app-name} | {description} | files: index.html, update.py, data.json

4. Tell the user: "Your app is ready. Open it at /apps/{agent_id}/{task_id}/{app-name}"

RULES FOR BUILDING APPS:
- Keep all HTML/CSS/JS in index.html (no external CDN dependencies unless necessary)
- Python scripts must print a JSON object to stdout as their final output
- Python scripts can read data files from the current directory (they're passed as workspace files)
- Python scripts write output files to the current directory — they get pushed back to storage automatically
- Agent secrets are available in Python scripts via os.environ['KEY_NAME']
- Always seed data.json with realistic initial data so the app works immediately

EXAMPLE — price simulator:
/workspace/apps/price-sim/index.html  → Chart.js chart, refresh button calls __APP__.run("update.py")
/workspace/apps/price-sim/update.py  → generates new OHLC data, writes data.json, prints {"status": "updated"}  
/workspace/apps/price-sim/data.json  → [{"t": "...", "o": 100, "h": 105, "l": 98, "c": 103}, ...]
"""


def build_app_context_block(apps_md: str) -> str:
    if not apps_md:
        return APP_BUILDER_INSTRUCTIONS + "\n\nNo apps built yet for this task."
    return APP_BUILDER_INSTRUCTIONS + f"\n\n══ EXISTING APPS ══\n{apps_md}"