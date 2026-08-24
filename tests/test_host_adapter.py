import os
import time
import pytest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from scripts._lib.core.agent_schema import (
    AgentRequest, AgentHandle, AgentResult,
    AgentTimeoutError, AgentCancelledError, AgentNotSupportedError,
    AgentStatus, CapabilitySupport, ConfirmationRequest, HostCapabilities
)
from scripts._lib.core.host_adapter import FakeHostAdapter

def test_fake_host_adapter_capabilities_immutable():
    adapter = FakeHostAdapter()
    caps = adapter.detect_capabilities()

    with pytest.raises(FrozenInstanceError):
        caps.is_real_host = True

    caps2 = adapter.detect_capabilities()
    assert caps2.is_real_host is False
    assert caps2.max_concurrent_agents == 0

def test_fake_host_adapter_capabilities_zero_write():
    """
    能力探测必须是零写入。我们通过 mock builtins.open 和 os.makedirs 来确保调用即失败。
    """
    adapter = FakeHostAdapter()

    with patch('builtins.open', side_effect=RuntimeError("Zero-write violation: open() called")), \
         patch('os.makedirs', side_effect=RuntimeError("Zero-write violation: makedirs() called")), \
         patch('os.mkdir', side_effect=RuntimeError("Zero-write violation: mkdir() called")):
        caps = adapter.detect_capabilities()

    assert caps.is_real_host is False

def test_fake_host_adapter_dispatch_namespace():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="codex-real-looking-123",
        prompt="Do something",
        role="DEV",
        workspace_dir="/tmp/fake"
    )

    handle = adapter.dispatch_agent(req)
    # The session_id must not be the caller's requested ID
    assert handle.session_id != "codex-real-looking-123"
    assert handle.session_id.startswith("fake-session:")

def test_fake_host_adapter_forged_handle_rejected():
    adapter = FakeHostAdapter()

    forged_handle = AgentHandle(
        session_id="fake-session:forged-id",
        host_id="fake_host",
        is_real_host=False
    )
    with pytest.raises(ValueError, match="Session fake-session:forged-id not found or forged."):
        adapter.wait_for_result(forged_handle)

    real_claiming_handle = AgentHandle(
        session_id="some-id",
        host_id="fake_host",
        is_real_host=True
    )
    with pytest.raises(ValueError, match="claims to be a real host"):
        adapter.cancel_agent(real_claiming_handle)

    wrong_host_handle = AgentHandle(
        session_id="some-id",
        host_id="other_host",
        is_real_host=False
    )
    with pytest.raises(ValueError, match="Invalid host_id: other_host"):
        adapter.wait_for_result(wrong_host_handle)

def test_fake_host_adapter_timeout_semantics():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test",
        prompt="Test timeout",
        role="QA",
        workspace_dir="/tmp/fake"
    )
    handle = adapter.dispatch_agent(req)

    # Fake adapter has a simulated latency of 0.05s
    start = time.time()
    with pytest.raises(AgentTimeoutError):
        adapter.wait_for_result(handle, timeout_seconds=0.01)

    # Now it should succeed if we give it enough time
    # Actually wait_for_result will just sleep for the remaining latency
    result = adapter.wait_for_result(handle, timeout_seconds=0.1)
    assert result.status == AgentStatus.SUCCESS

def test_fake_host_confirmation_fails_closed():
    adapter = FakeHostAdapter()
    req = ConfirmationRequest(
        request_id="conf_1",
        prompt="Are you sure?",
        options=["yes", "no"]
    )
    with pytest.raises(AgentNotSupportedError):
        adapter.request_confirmation(req)

def test_fake_host_adapter_cancel():
    adapter = FakeHostAdapter()
    req = AgentRequest(
        session_id="test_session_2",
        prompt="Do something else",
        role="REVIEWER",
        workspace_dir="/tmp/fake"
    )
    handle = adapter.dispatch_agent(req)

    assert adapter.cancel_agent(handle) is True
    with pytest.raises(AgentCancelledError):
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
