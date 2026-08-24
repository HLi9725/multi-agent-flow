import json
import os
import subprocess
import time
import sys
import requests

SERVER_SCRIPT = os.path.join("scripts", "start_kanban_server.py")

def test_agent_paths_api():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["KANBAN_PORT"] = "32956"
    p = subprocess.Popen([sys.executable, SERVER_SCRIPT, "--allow-remote"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    
    try:
        # GET /api/agent/paths
        res = requests.get("http://127.0.0.1:32956/api/agent/paths")
        assert res.status_code == 200
        data = res.json()["data"]
        
        # Verify required status keys
        assert "antigravity_ide" in data
        assert data["antigravity_ide"]["status"] == "static_only"
        assert data["antigravity_ide"]["global_skill_target"] == "~/.gemini/config/skills/{skill_name}"
        
        assert "antigravity_cli" in data
        assert data["antigravity_cli"]["status"] == "static_only"
        
        assert "opencode" in data
        assert data["opencode"]["status"] == "not_run"
        
        assert "zcode" in data
        assert data["zcode"]["status"] == "not_run"
        
    finally:
        p.terminate()
        p.communicate()
