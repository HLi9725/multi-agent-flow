import os
import time
import pytest
from scripts._lib.core.agent_schema import (
    AgentRequest, AgentHandle, AgentResult,
    AgentTimeoutError, AgentCancelledError, AgentStatus,
    CapabilitySupport, ConfirmationRequest
)
from scripts._lib.core.host_adapter import FakeHostAdapter

def test_fake_host_adapter_capabilities_zero_write(tmp_path):
    """
    能力探测必须是零写入。
    """
    adapter = FakeHostAdapter()
    
    # 记录文件树状态
    def get_tree(p):
        return {os.path.relpath(os.path.join(r, f), p) for r, d, files in os.walk(p) for f in files}
        
    tree_before = get_tree(str(tmp_path))
    
    caps = adapter.detect_capabilities()
    
    tree_after = get_tree(str(tmp_path))
    assert tree_before == tree_after, "Capability detection must be zero-write"
    
    assert caps.is_real_host is False
    assert caps.supports_mcp == CapabilitySupport.UNSUPPORTED
    assert caps.supports_worktree == CapabilitySupport.UNSUPPORTED

def test_fake_host_adapter_dispatch_and_success():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test_session_1",
        prompt="Do something",
        role="DEV",
        workspace_dir="/tmp/fake"
    )
    
    handle = adapter.dispatch_agent(req)
    assert handle.is_real_host is False
    assert handle.session_id == "test_session_1"
    
    result = adapter.wait_for_result(handle)
    assert result.is_real_host is False
    assert result.status == AgentStatus.SUCCESS
    assert "Simulated FakeHost" in result.output

def test_fake_host_adapter_cancel():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test_session_2",
        prompt="Do something else",
        role="REVIEWER",
        workspace_dir="/tmp/fake"
    )
    handle = adapter.dispatch_agent(req)
    
    # Cancel it
    assert adapter.cancel_agent(handle) is True
    
    with pytest.raises(AgentCancelledError):
        adapter.wait_for_result(handle)

def test_fake_host_adapter_timeout():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test_session_3",
        prompt="Simulate TIMEOUT condition",
        role="QA",
        workspace_dir="/tmp/fake"
    )
    handle = adapter.dispatch_agent(req)
    
    with pytest.raises(AgentTimeoutError):
        adapter.wait_for_result(handle)

def test_fake_host_adapter_partial():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test_session_4",
        prompt="Simulate PARTIAL condition",
        role="QA",
        workspace_dir="/tmp/fake"
    )
    handle = adapter.dispatch_agent(req)
    
    result = adapter.wait_for_result(handle)
    assert result.status == AgentStatus.PARTIAL
    assert len(result.partial_results) > 0
    assert result.is_real_host is False

def test_fake_host_confirmation():
    adapter = FakeHostAdapter()
    req = ConfirmationRequest(
        request_id="conf_1",
        prompt="Are you sure?",
        options=["yes", "no"]
    )
    result = adapter.request_confirmation(req)
    assert result.is_real_host is False
    assert result.is_confirmed is True
    assert result.selected_option == "yes"
