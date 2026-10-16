"""Cooperative run cancellation and a run-owned async transport loop."""

from __future__ import annotations

import asyncio
import queue
import threading
from contextvars import ContextVar


class RunInterrupted(Exception):
    """A cancellation signal, never a tool failure or model fallback."""


CURRENT_RUN: ContextVar[RunControl | None] = ContextVar("mokioclaw_run", default=None)


class RunControl:
    def __init__(self) -> None:
        from mokioclaw.core.run_inbox import RunInbox

        self.inbox = RunInbox()
        self.pending_stream_ends = {}
        self.cancel_event = threading.Event()
        self.status = "running"
        self.event_sink = None
        self._lock = threading.RLock()
        self._loop = None
        self._thread = None
        self._closed = False

    def cancel(self) -> bool:
        with self._lock:
            if self.status != "running":
                return False
            self.cancel_event.set()
            return True

    def finish(self, status: str) -> str:
        with self._lock:
            if self.status == "running":
                self.status = "interrupted" if self.cancel_event.is_set() else status
            return self.status

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise RunInterrupted("Run cancelled by user")

    def _event_loop(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Run transport loop is closed")
            if self._loop is None:
                ready = threading.Event()

                def serve():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._loop = loop
                    ready.set()
                    try:
                        loop.run_forever()
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    finally:
                        loop.close()

                self._thread = threading.Thread(target=serve, name="mokio-transport", daemon=True)
                self._thread.start()
                ready.wait()
            return self._loop

    def iter_async(self, factory):
        """Yield on the caller thread; cancellation also covers a silent stream."""
        self.check()
        chunks = queue.Queue(maxsize=64)
        stopped = threading.Event()
        cleaned = threading.Event()

        async def put(item):
            while not stopped.is_set():
                try:
                    chunks.put_nowait(item)
                    return
                except queue.Full:
                    await asyncio.sleep(0.01)

        async def produce():
            iterator = None
            try:
                iterator = factory()
                async for item in iterator:
                    await put(("item", item))
                await put(("end", None))
            except BaseException as exc:  # noqa: BLE001 - forward async cancellation and failures to the caller
                if not stopped.is_set():
                    await put(("error", exc))
            finally:
                try:
                    if iterator is not None and hasattr(iterator, "aclose"):
                        await iterator.aclose()
                finally:
                    cleaned.set()

        loop = self._event_loop()
        future = asyncio.run_coroutine_threadsafe(produce(), loop)
        try:
            while True:
                self.check()
                try:
                    kind, item = chunks.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.check()
                if kind == "end":
                    return
                if kind == "error":
                    raise item
                yield item
        finally:
            stopped.set()
            if not cleaned.is_set():
                future.cancel()
                if not cleaned.wait(2):
                    raise RuntimeError("Async transport did not close after cancellation")

    def run_async(self, factory):
        async def once():
            yield await factory()

        iterator = self.iter_async(once)
        try:
            return next(iterator)
        finally:
            iterator.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("Run transport loop did not stop")


def run_control(runtime=None) -> RunControl | None:
    return getattr(runtime, "run_control", None) or CURRENT_RUN.get()


def check_run(runtime=None) -> None:
    control = run_control(runtime)
    if control is not None:
        control.check()
