"""Role-specific model selection and run-level token accounting."""

import copy
import time
from contextvars import ContextVar

_SELECTION = ContextVar("mokio_model_selection", default=None)
REQUEST_BUDGET = ContextVar("mokio_request_budget", default=None)


class RunBudgetExceeded(BaseException):
    def __init__(self, message="Run token budget exhausted"):
        super().__init__(message)
        self.state = {}


def create_role_model(factory, runtime, role):
    if runtime is None:
        return factory()
    profiles = runtime.settings.get("model_profiles", {})
    roles = runtime.settings.get("model_roles", {})
    name = runtime.model_profile or roles.get(runtime.active_agent_name or role) or roles.get(role)
    if name is not None and name not in profiles:
        raise ValueError(f"unknown model profile: {name}")
    config = profiles.get(name, {})
    if config and (
        int(config.get("context_tokens", 0)) <= int(config.get("output_tokens", 0))
        or int(config.get("output_tokens", 0)) <= 0
    ):
        raise ValueError("model profiles require context_tokens > output_tokens > 0")
    token = _SELECTION.set(config)
    try:
        return factory()
    finally:
        _SELECTION.reset(token)


def profile_for(runnable):
    while getattr(runnable, "bound", None) is not None:
        runnable = runnable.bound
    return getattr(runnable, "mokio_profile", {})


def begin_request(runtime, role, snapshot, budget):
    if runtime is None:
        return time.monotonic()
    metrics = runtime.model_metrics
    reserve = snapshot.input_tokens + budget.output
    limit = runtime.run_token_limit
    if limit and metrics.get("total_tokens", 0) + reserve > limit:
        error = RunBudgetExceeded()
        error.state = {
            key: copy.deepcopy(value)
            for key, value in (runtime.foreground_state or {}).items()
            if key not in {"runtime", "messages"}
        }
        raise error
    metrics["requests"] = metrics.get("requests", 0) + 1
    return time.monotonic()


def finish_request(
    runtime, response, role, input_estimate, started, writer, status, stream_id=None
):
    from mokioclaw.providers.usage import normalize_usage

    usage = normalize_usage(response, input_estimate)
    if runtime is None and usage.estimated:
        return
    incoming, outgoing = usage.input_tokens, usage.output_tokens
    reported = not usage.estimated
    elapsed = round((time.monotonic() - started) * 1000, 3)
    event = {
        "type": "model_usage",
        "agent": role,
        "scope": "run",
        "input_tokens": incoming,
        "output_tokens": outgoing,
        "total_tokens": incoming + outgoing,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "usage_source": usage.source,
        "reported": reported,
        "estimated": not reported,
        "duration_ms": elapsed,
        "status": status,
    }
    if stream_id:
        event["stream_id"] = stream_id
    if runtime is not None:
        metrics = runtime.model_metrics
        for target in (metrics, metrics.setdefault("roles", {}).setdefault(role, {})):
            target["estimated"] = target.get("estimated", False) or not reported
            for key in ("input_tokens", "output_tokens", "total_tokens", "duration_ms"):
                target[key] = target.get(key, 0) + event[key]
            for key in ("cache_read_tokens", "cache_creation_tokens"):
                value = event[key]
                if value is not None:
                    target[key] = (target.get(key) or 0) + value
                else:
                    target.setdefault(key, None)
                    target["cache_usage_incomplete"] = True
        by_role = metrics["roles"][role]
        by_role["requests"] = by_role.get("requests", 0) + 1
        metrics["estimated"] = metrics.get("estimated", False) or not reported
        event["run_total_tokens"] = metrics["total_tokens"]
    if writer:
        writer(event)
