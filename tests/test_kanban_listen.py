import sys
import os
import subprocess
import time
import socket
import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(SCRIPT_DIR, '..', 'scripts', 'start_kanban_server.py')

def test_default_listen_address():
    # Start server with default args (should be 127.0.0.1)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["KANBAN_PORT"] = "32950"  # Pick a random port for test
    p = subprocess.Popen([sys.executable, SERVER_SCRIPT], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    time.sleep(2)
    p.kill()
    out, err = p.communicate()
    assert "http://127.0.0.1:32950/" in out
    assert "局域网访问地址" not in out

def test_remote_listen_rejected_without_flag():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.Popen([sys.executable, SERVER_SCRIPT, "--host", "0.0.0.0"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    out, err = p.communicate()
    assert p.returncode != 0
    assert "非本机监听 (0.0.0.0) 必须显式添加 --allow-remote 参数" in out

def test_remote_listen_with_flag():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["KANBAN_PORT"] = "32951"
    p = subprocess.Popen([sys.executable, SERVER_SCRIPT, "--host", "0.0.0.0", "--allow-remote"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    time.sleep(2)
    p.kill()
    out, err = p.communicate()
    assert "局域网远程访问" in out
    assert "局域网访问地址" in out
