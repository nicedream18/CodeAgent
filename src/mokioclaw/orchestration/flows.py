"""CrewAI event routing for entry, full and light execution paths."""

from __future__ import annotations

from crewai.flow.flow import listen, or_, router, start

from mokioclaw.orchestration.runtime import ControlledFlow


class EntryFlow(ControlledFlow):
    @start()
    def intent_router(self) -> None:
        self.run_step("intent_router")

    @router(intent_router)
    def route_intent(self) -> str:
        return "respond" if self.payload.get("intent_route") == "chat" else "workflow"

    @listen("respond")
    def chat_responder(self) -> None:
        self.run_step("chat_responder")


class FullFlow(ControlledFlow):
    @start()
    def begin(self) -> None:
        self.state.next_node = "planner"

    @listen("run_planner")
    def planner(self) -> None:
        self.run_step("planner")
        self.state.next_node = "context_monitor"

    @listen("run_context_monitor")
    def context_monitor(self) -> None:
        from mokioclaw.graph.nodes import context_monitor_route

        self.run_step("context_monitor")
        self.state.next_node = context_monitor_route(self.payload)

    @listen("run_context_compressor")
    def context_compressor(self) -> None:
        from mokioclaw.graph.nodes import context_compressor_route

        self.run_step("context_compressor")
        self.state.next_node = context_compressor_route(self.payload)

    @listen("run_testAgent")
    def test_agent(self) -> None:
        self.run_step("testAgent")
        self.state.next_node = "context_monitor"

    @listen("run_verifier")
    def verifier(self) -> None:
        self.run_step("verifier")
        self.state.next_node = "context_monitor"

    @router(or_(begin, planner, test_agent, context_monitor, context_compressor, verifier))
    def next_step(self) -> str:
        node = str(self.state.next_node)
        if node not in {"planner", "testAgent", "context_monitor", "context_compressor", "verifier", "final"}:
            raise ValueError(f"Unsupported full Flow route: {node}")
        return "run_" + node

    @listen("run_final")
    def final(self) -> None:
        self.run_step("final")


class LightFlow(ControlledFlow):
    @start()
    def light_executor(self) -> None:
        self.run_step("light_executor")

    @router(light_executor)
    def after_implementation(self) -> str:
        return "upgrade" if self.payload.get("workflow_escalate") else "test"

    @listen("test")
    def test_agent(self) -> None:
        self.run_step("testAgent")

    @listen(test_agent)
    def verifier(self) -> None:
        self.run_step("verifier")

    @router(verifier)
    def after_verification(self) -> str:
        return "finish" if self.payload.get("passed") else "upgrade"

    @listen("upgrade")
    def full_executor(self) -> None:
        self.run_step("full_executor")

    @listen("finish")
    def final(self) -> None:
        self.run_step("final")
