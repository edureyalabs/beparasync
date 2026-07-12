# beparasync/test_chat.py
# Run this locally to test the chat pipeline without the frontend
# Usage: python test_chat.py

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

# Test what the agent sees in its system prompt
async def test_system_prompt():
    from routes.chat import (
        load_agent, load_contexts, load_platform_tools,
        build_chat_system_prompt
    )
    from memory_manager import get_memory
    from workspace_manager import get_task_md
    from app_context import get_apps_md

    AGENT_ID = "7ccc100b-4340-4f5a-8d35-64484b34277b"
    ORG_ID   = "761544ec-17cb-4472-a471-afade56cd585"
    TASK_ID  = "d256ca41-df4f-4e07-95db-f26b7f70fb7f"

    print("=== Loading agent ===")
    agent = load_agent(AGENT_ID)
    print(f"Agent: {agent['name']}")

    print("\n=== Loading platform tools ===")
    platform_tools = load_platform_tools(AGENT_ID)
    print(f"Platform tools: {platform_tools}")

    print("\n=== Loading memory ===")
    memory = get_memory(AGENT_ID)
    print(f"Memory keys: {list(memory.keys())}")

    print("\n=== Loading task_md ===")
    task_md = get_task_md(ORG_ID, AGENT_ID, TASK_ID)
    print(f"TASK.md length: {len(task_md)} chars")

    print("\n=== Loading apps_md ===")
    apps_md = get_apps_md(ORG_ID, AGENT_ID, TASK_ID)
    print(f"APPS.md length: {len(apps_md)} chars")
    print(f"APPS.md content: {apps_md[:200] if apps_md else '(empty)'}")

    print("\n=== Building system prompt ===")
    contexts = load_contexts(AGENT_ID)
    prompt = build_chat_system_prompt(
        agent=agent,
        contexts=contexts,
        memory=memory,
        task_md=task_md,
        apps_md=apps_md,
        resolved_task_id=TASK_ID,
        agent_secret_keys=[],
        agent_id=AGENT_ID,
        task_id=TASK_ID,
    )

    print(f"\nSystem prompt length: {len(prompt)} chars")
    print("\n=== APP BUILDER SECTION ===")
    # Find the app builder section
    if "APP BUILDER" in prompt:
        start = prompt.index("APP BUILDER")
        print(prompt[start:start+800])
    else:
        print("WARNING: APP BUILDER section NOT FOUND in system prompt!")

    print("\n=== APP URL SECTION ===")
    if "APP URLS" in prompt:
        start = prompt.index("APP URLS")
        print(prompt[start:start+300])
    else:
        print("WARNING: APP URLS section NOT FOUND in system prompt!")


async def test_workspace_push():
    """Test that files written to subdirectories get pushed correctly."""
    import tempfile
    import shutil
    from pathlib import Path
    from workspace_manager import pull_workspace, push_workspace, snapshot_hashes

    ORG_ID   = "761544ec-17cb-4472-a471-afade56cd585"
    AGENT_ID = "7ccc100b-4340-4f5a-8d35-64484b34277b"
    TASK_ID  = "d256ca41-df4f-4e07-95db-f26b7f70fb7f"

    workspace_dir = Path(tempfile.mkdtemp())
    print(f"\n=== Testing workspace push to {workspace_dir} ===")

    try:
        pull_workspace(ORG_ID, AGENT_ID, TASK_ID, workspace_dir)
        pre_hashes = snapshot_hashes(workspace_dir)
        print(f"Files before: {list(pre_hashes.keys())}")

        # Simulate agent writing app files
        app_dir = workspace_dir / "apps" / "test-app"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "index.html").write_text("<html><body>Test App</body></html>")
        (app_dir / "data.json").write_text('[{"title": "test"}]')
        (app_dir / "update.py").write_text('import json\nprint(json.dumps({"status": "ok"}))')

        post_files = [str(f.relative_to(workspace_dir)) for f in workspace_dir.rglob("*") if f.is_file()]
        print(f"Files after writing: {post_files}")

        push_workspace(ORG_ID, AGENT_ID, TASK_ID, workspace_dir, pre_hashes)
        print("push_workspace completed successfully")

    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


if __name__ == "__main__":
    print("TEST 1: System prompt check")
    asyncio.run(test_system_prompt())

    print("\n" + "="*60)
    print("TEST 2: Workspace push with subdirectories")
    asyncio.run(test_workspace_push())