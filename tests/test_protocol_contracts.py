import json
from io import BytesIO

from mokioclaw.core.daemon import AgentDaemon
from mokioclaw.core.protocol import CAPABILITIES, RunOptions, validate_event


def test_protocol_validates_before_starting_run(tmp_path):
    daemon = AgentDaemon(tmp_path)
    try:
        for params in [
            {"mode": "invalid"},
            {"options": {"max_attempts": 0}},
            {"options": {"workflow_mode": "unknown"}},
        ]:
            response = daemon.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "start_run",
                    "params": {"token": daemon.token, **params},
                },
                BytesIO(),
            )
            assert response["error"]["code"] == -32602
        assert not daemon.runs
        assert (
            daemon.dispatch(
                {"jsonrpc": "2.0", "id": 2, "method": "unknown", "params": {"token": daemon.token}},
                BytesIO(),
            )["error"]["code"]
            == -32601
        )
        result = daemon.dispatch(
            {"jsonrpc": "2.0", "id": 3, "method": "status", "params": {"token": daemon.token}},
            BytesIO(),
        )["result"]
        assert result["protocol_version"] == 1 and result["capabilities"] == CAPABILITIES
    finally:
        daemon.executor.shutdown(wait=True)


def test_legacy_options_and_extensible_events_keep_wire_shape():
    assert RunOptions.model_validate({"task": "test"}).model_dump(exclude_unset=True) == {
        "task": "test"
    }
    raw = {
        "type": "tool_result",
        "name": "custom",
        "result": {"ok": True},
        "event_id": "e",
        "custom_metadata": 42,
    }
    assert validate_event(raw) == raw
    assert json.dumps(raw)


def test_invalid_types_and_missing_task_never_start_runs(tmp_path):
    daemon = AgentDaemon(tmp_path)
    try:
        for options in [
            {},
            {"task": 42},
            {"task": "x", "max_attempts": "2"},
            {"task": "x", "unknown": True},
        ]:
            reply = daemon.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "start_run",
                    "params": {"token": daemon.token, "options": options},
                },
                BytesIO(),
            )
            assert reply["error"]["code"] == -32602
        assert not daemon.runs
        reply = daemon.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "protocol_version": 2,
                "method": "status",
                "params": {"token": daemon.token},
            },
            BytesIO(),
        )
        assert reply["error"]["code"] == -32600
    finally:
        daemon.executor.shutdown(wait=True)


def test_new_client_rejects_unsupported_explicit_skill(monkeypatch):
    import pytest

    from mokioclaw.core import daemon_client

    seen = []
    monkeypatch.setattr(
        daemon_client, "ensure_daemon", lambda **kwargs: {"host": "127.0.0.1", "port": 1}
    )

    def request(address, method, params):
        seen.append(method)
        return {"runs": []}

    monkeypatch.setattr(daemon_client, "_request", request)
    with pytest.raises(RuntimeError, match="explicit_skill"):
        list(daemon_client.run_via_daemon("run", "test", options={"skill": "review"}))
    assert seen == ["status"]


def test_invalid_envelope_is_not_broken_by_transport_logging(tmp_path):
    daemon = AgentDaemon(tmp_path)
    try:
        for request in [
            [],
            {"id": [], "method": 42},
            {"id": True, "method": "status"},
            {"id": 1, "method": 42},
        ]:
            reply = daemon.dispatch(request, BytesIO())
            assert reply["error"]["code"] == -32600
            assert reply["id"] is None or reply["id"] == 1
        assert not daemon.runs
    finally:
        daemon.executor.shutdown(wait=True)
