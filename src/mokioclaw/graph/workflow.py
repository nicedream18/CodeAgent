"""Public workflow builders backed by CrewAI Flows.

The historical module path remains import-compatible for extensions and resumes.
"""

from __future__ import annotations

from mokioclaw.graph.nodes import (
    chat_responder_node,
    context_compressor_node,
    context_monitor_node,
    final_node,
    intent_router_node,
    planner_node,
    test_agent_node,
    verifier_node,
)
from mokioclaw.orchestration.flows import EntryFlow, FullFlow
from mokioclaw.orchestration.runtime import CrewWorkflow


def build_workflow():
    return build_complex_workflow()


def build_complex_workflow():
    return CrewWorkflow(FullFlow, {
        "planner": planner_node,
        "testAgent": test_agent_node,
        "context_monitor": context_monitor_node,
        "context_compressor": context_compressor_node,
        "verifier": verifier_node,
        "final": final_node,
    })


def build_entry_workflow():
    return CrewWorkflow(EntryFlow, {
        "intent_router": intent_router_node,
        "chat_responder": chat_responder_node,
    })
