import time
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest

from scripts._lib.core.agent_schema import (
    AgentCancelledError,
    AgentInvalidHandleError,
    AgentNotSupportedError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    AgentTimeoutError,
    CapabilitySupport,
    ConfirmationRequest,
    HostCapabilities,
)
from scripts._lib.core.host_adapter import FakeHostAdapter


def test_contract_schemas_are_deeply_immutable():
    caps = HostCapabilities(extra={"nested": {"items": [1, 2]}})
    with pytest.raises(FrozenInstanceError):
        caps.is_real_host = True
    with pytest.raises(TypeError):
        caps.extra["poison"] = True
    with pytest.raises(TypeError):
        caps.extra["nested"]["poison"] = True
    assert caps.extra["nested"]["items"] == (1, 2)

    request = AgentRequest(
        session_id="caller-request",
        prompt="Do something",
        role="DEV",
        workspace_dir="/tmp/fake",
        capabilities_required=["worktree"],
        extra_context={"nested": {"roles": ["DEV"]}},
    )
    assert request.capabilities_required == ("worktree",)
    assert request.extra_context["nested"]["roles"] == ("DEV",)
    with pytest.raises(TypeError):
        request.extra_context["new"] = "value"

    result = AgentResult(
        session_id="fake-session:test",
        status=AgentStatus.PARTIAL,
        output="partial",
        partial_results=[{"artifacts": ["a.txt"]}],
    )
    assert result.partial_results[0]["artifacts"] == ("a.txt",)
    with pytest.raises(TypeError):
        result.partial_results[0]["new"] = "value"

    confirmation = ConfirmationRequest(
        request_id="confirm-1",
        prompt="Continue?",
        options=["yes", "no"],
    )
    assert confirmation.options == ("yes", "no")


def test_fake_host_adapter_capabilities_fail_closed():
    adapter = FakeHostAdapter()
    caps = adapter.detect_capabilities()

    assert caps.is_real_host is False
    assert caps.max_concurrent_agents == 0
    assert caps.supports_real_subagents == CapabilitySupport.UNSUPPORTED
    assert caps.supports_parallelism == CapabilitySupport.UNSUPPORTED
    assert caps.supports_worktree == CapabilitySupport.UNSUPPORTED
    assert caps.supports_mcp == CapabilitySupport.UNSUPPORTED


def test_fake_host_adapter_capabilities_have_no_io_side_effects():
    adapter = FakeHostAdapter()
    blocked_targets = (
        "builtins.open",
        "os.open",
        "os.mkdir",
        "os.makedirs",
        "os.replace",
        "pathlib.Path.open",
        "pathlib.Path.mkdir",
        "pathlib.Path.touch",
        "pathlib.Path.write_bytes",
        "pathlib.Path.write_text",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
        "subprocess.Popen",
        "subprocess.run",
        "socket.create_connection",
        "socket.socket",
        "urllib.request.urlopen",
    )

    with ExitStack() as stack:
        for target in blocked_targets:
            stack.enter_context(
                patch(target, side_effect=RuntimeError(f"Side effect attempted: {target}"))
            )
        caps = adapter.detect_capabilities()

    assert caps.is_real_host is False


def _request(*, timeout_seconds=3600, prompt="Do something"):
    return AgentRequest(
        session_id="caller-controlled-id",
        prompt=prompt,
        role="DEV",
        workspace_dir="/tmp/fake",
        timeout_seconds=timeout_seconds,
    )


def test_fake_host_adapter_dispatch_uses_internal_namespace_and_bearer_handle():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request())

    assert handle.session_id != "caller-controlled-id"
    assert handle.session_id.startswith("fake-session:")
    assert handle.adapter_instance_id.startswith("fake-adapter:")
    assert handle.invocation_token
    assert handle.is_real_host is False

    # AgentHandle is an immutable bearer capability: an exact serialized copy is valid.
    copied_handle = replace(handle)
    result = adapter.wait_for_result(copied_handle)
    assert result.status == AgentStatus.SUCCESS
    assert result.is_real_host is False


def test_fake_host_adapter_rejects_unknown_cross_host_and_tampered_handles():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request())

    unknown = replace(handle, session_id="fake-session:unknown")
    with pytest.raises(AgentInvalidHandleError, match="not found or forged"):
        adapter.wait_for_result(unknown)

    wrong_host = replace(handle, host_id="other_host")
    with pytest.raises(AgentInvalidHandleError, match="Invalid host_id"):
        adapter.wait_for_result(wrong_host)

    real_claim = replace(handle, is_real_host=True)
    with pytest.raises(AgentInvalidHandleError, match="claims to be a real host"):
        adapter.cancel_agent(real_claim)

    tampered_token = replace(handle, invocation_token="tampered")
    with pytest.raises(AgentInvalidHandleError, match="invocation token"):
        adapter.wait_for_result(tampered_token)

    other_adapter = FakeHostAdapter()
    with pytest.raises(AgentInvalidHandleError, match="another adapter instance"):
        other_adapter.wait_for_result(handle)


def test_fake_host_adapter_request_timeout_is_default_and_can_be_overridden():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request(timeout_seconds=0.005))

    with pytest.raises(AgentTimeoutError):
        adapter.wait_for_result(handle)

    result = adapter.wait_for_result(handle, timeout_seconds=0.1)
    assert result.status == AgentStatus.SUCCESS


def test_fake_host_adapter_timeout_uses_remaining_time():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request(timeout_seconds=1))

    time.sleep(0.04)
    result = adapter.wait_for_result(handle, timeout_seconds=0.02)
    assert result.status == AgentStatus.SUCCESS


def test_fake_host_adapter_explicit_timeout_and_negative_timeout_validation():
    with pytest.raises(ValueError, match="finite non-negative"):
        _request(timeout_seconds=-1)
    with pytest.raises(ValueError, match="finite non-negative"):
        _request(timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="finite non-negative"):
        _request(timeout_seconds=True)

    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request())
    with pytest.raises(ValueError, match="finite non-negative"):
        adapter.wait_for_result(handle, timeout_seconds=-1)
    with pytest.raises(AgentTimeoutError):
        adapter.wait_for_result(handle, timeout_seconds=0.001)


def test_fake_host_confirmation_fails_closed():
    adapter = FakeHostAdapter()
    req = ConfirmationRequest(
        request_id="conf_1",
        prompt="Are you sure?",
        options=["yes", "no"],
    )
    with pytest.raises(AgentNotSupportedError):
        adapter.request_confirmation(req)


def test_fake_host_adapter_cancel():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request())

    assert adapter.cancel_agent(handle) is True
    with pytest.raises(AgentCancelledError):
        adapter.wait_for_result(handle)


def test_fake_host_adapter_partial_result_is_immutable_and_cached():
    adapter = FakeHostAdapter()
    handle = adapter.dispatch_agent(_request(prompt="Simulate PARTIAL condition"))

    result = adapter.wait_for_result(handle)
    assert result.status == AgentStatus.PARTIAL
    assert result.partial_results == ("part1", "part2")
    assert result.is_real_host is False
    assert adapter.wait_for_result(handle) is result
