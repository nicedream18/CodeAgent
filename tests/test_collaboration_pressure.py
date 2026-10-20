import json

import pytest

from mokioclaw.evaluation.context_pressure import run_case


@pytest.mark.parametrize("scenario", ["long_file", "command_tail", "repair_correction"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("manual", [True, False])
def test_actual_context_pressure(tmp_path, scenario, enabled, manual):
    result = run_case(tmp_path, scenario, enabled=enabled, manual=manual)
    assert result["simulation"] and not result["real_model"]
    assert max(result["estimated_inputs_before_governance"]) >= result["budget"]["high"]
    assert max(result["actual_request_input_estimates"]) <= result["budget"]["hard"]
    assert result["recoveries_used"] == 1 and result["guard_history_count"] > 0
    assert result["prior_history_retained"]
    assert len(result["actual_request_input_estimates"]) <= 40
    if enabled:
        assert result["artifact_passed"] and result["execution_status"] == "finished"
        assert result["compression_count"] >= 1
        assert len(result["summary_input_estimates"]) == result["compression_count"]
        if manual:
            assert any(e["reason"] == "manual" for e in result["compressions"])
        if scenario == "repair_correction":
            assert result["correction_applied"]
            facts = [
                json.loads(p.read_text())
                for p in (tmp_path / ".mokioclaw/context").rglob("facts-*.json")
            ]
            assert any(f.get("last_error") for f in facts)
            assert all(f["todos"][0]["status"] == "pending" for f in facts)
    else:
        assert not result["artifact_passed"] and result["execution_status"] == "failed"
        assert result["compression_count"] == 0
        assert max(result["estimated_inputs_before_governance"]) > result["budget"]["hard"]
        assert result["stop_reason"] == "context_budget_exceeded"
    assert (tmp_path / result["checkpoint"]).exists()
    if scenario == "command_tail":
        logs = list((tmp_path / ".mokioclaw").rglob("bash-stdout-*.log"))
        assert logs and any("E_TAIL:" in p.read_text() for p in logs)


def test_real_docker_context_pressure(tmp_path):
    import os

    if os.getenv("MOKIO_TEST_DOCKER") != "1":
        pytest.skip("explicit real Docker integration suite only")
    from mokioclaw.core.execution_backend import doctor

    image = os.getenv("MOKIO_DOCKER_IMAGE", "mokioclaw-execution:0.2.0")
    check = doctor("docker", image)
    assert check["ok"], check
    for scenario in ("command_tail", "repair_correction"):
        for enabled in (True, False):
            result = run_case(
                tmp_path / f"{scenario}-{enabled}",
                scenario,
                enabled=enabled,
                manual=scenario == "repair_correction",
                execution_backend="docker",
                docker_image=check["image_digest"],
            )
            assert result["image_digest"] == check["image_digest"]
            assert result["artifact_passed"] == enabled
            assert (
                result["compression_count"] >= 1
                if enabled
                else result["stop_reason"] == "context_budget_exceeded"
            )

    # Each invocation creates a fresh container ID/log path. These must not
    # manufacture progress or allow the fourth unchanged failure to execute.
    from mokioclaw.core.loop_guard import LoopRecovery
    from mokioclaw.core.state import RuntimeState
    from mokioclaw.tools.execution import execute_tool
    from mokioclaw.tools.registry import build_tool
    area = tmp_path / "repeat-container"
    area.mkdir()
    runtime = RuntimeState(workspace=area,execution_backend="docker",docker_image=check["image_digest"],approval_mode="auto")
    tool = build_tool(runtime,"BashTool")
    args = {"command":"python -c 'raise SystemExit(2)'"}
    for _ in range(3):
        assert not execute_tool(runtime,tool,args)["ok"]
    with pytest.raises(LoopRecovery,match="failed_call_loop"):
        execute_tool(runtime,tool,args)
    assert runtime.tool_execution_count == 3
