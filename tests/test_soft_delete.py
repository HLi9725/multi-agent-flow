import json
import os
import subprocess
import time
import sys
import requests

SERVER_SCRIPT = os.path.join("scripts", "start_kanban_server.py")

def test_soft_delete():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["KANBAN_PORT"] = "32955"
    p = subprocess.Popen([sys.executable, SERVER_SCRIPT, "--allow-remote"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    
    try:
        # Create a task
        res = requests.post("http://127.0.0.1:32955/api/tasks", json={"name": "Delete Me", "status": "待开始"})
        assert res.status_code == 200
        task_id = res.json()["data"]["id"]
        
        # Soft delete it
        res = requests.delete(f"http://127.0.0.1:32955/api/tasks/{task_id}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] == 1
        
        # Verify it's not in GET without include_deleted
        res = requests.get("http://127.0.0.1:32955/api/tasks")
        assert res.status_code == 200
        items = res.json()["data"]["items"]
        assert not any(t["id"] == task_id for t in items)
        
        # Verify it is in GET with include_deleted=true
        res = requests.get("http://127.0.0.1:32955/api/tasks?include_deleted=true")
        assert res.status_code == 200
        items = res.json()["data"]["items"]
        deleted_task = next(t for t in items if t["id"] == task_id)
        assert deleted_task["is_deleted"] is True
        
        # Restore it
        res = requests.post(f"http://127.0.0.1:32955/api/tasks/{task_id}/restore")
        assert res.status_code == 200
        assert res.json()["data"]["restored"] == 1
        
        # Verify it's back
        res = requests.get("http://127.0.0.1:32955/api/tasks")
        assert res.status_code == 200
        items = res.json()["data"]["items"]
        assert any(t["id"] == task_id for t in items)
        
    finally:
        p.terminate()
        p.communicate()
