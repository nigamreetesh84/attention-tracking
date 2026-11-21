"""The engine child process and the real launcher.

`ProcessLauncher` starts `engine_child_main` in a new process (spawn, as on
Windows). The child builds the engine from a `"module:function"` factory
path, serves it over IPC, and bumps a shared heartbeat counter from its main
loop. Killing the process, or freezing it, stops the counter; the supervisor
notices and relaunches.
"""

import contextlib
import ctypes
import importlib
import multiprocessing
import sys
import time
from collections.abc import Callable
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from typing import Final, Literal, Protocol

from ag_workstation.service.ipc_server import EventPublisher, IpcServer, StateAndHealth
from ag_workstation.service.transport import IpcTransport

HEARTBEAT_INTERVAL_S: Final = 0.25

EngineFactory = Callable[[EventPublisher], StateAndHealth]
"""Builds the engine in the child; wires the publisher as its event listener."""


class _Counter(Protocol):
    value: int


def resolve_factory(path: str) -> EngineFactory:
    """Import `"package.module:function"`."""
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("engine factory must be 'module:function'")
    factory: EngineFactory = getattr(importlib.import_module(module_name), attr)
    if not callable(factory):
        raise TypeError("engine factory is not callable")
    return factory


def engine_child_main(
    counter: _Counter,
    factory_path: str,
    transport: IpcTransport,
    interval_s: float = HEARTBEAT_INTERVAL_S,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_beats: int | None = None,
) -> None:
    """Child entry point: build, serve, beat. Runs until killed."""
    publisher = EventPublisher()
    engine = resolve_factory(factory_path)(publisher)
    server = IpcServer(transport, engine)
    publisher.bind(server)
    server.start()
    beats = 0
    try:
        while max_beats is None or beats < max_beats:
            counter.value += 1
            beats += 1
            sleep(interval_s)
    finally:
        server.stop()


class ProcessChild:
    """A `ChildProcess` backed by `multiprocessing.Process`."""

    def __init__(self, process: BaseProcess, counter: _Counter) -> None:
        self._process = process
        self._counter = counter

    @property
    def pid(self) -> int | None:
        """OS process id."""
        return self._process.pid

    @property
    def exitcode(self) -> int | None:
        """Exit code (negative signal number on POSIX) once ended."""
        return self._process.exitcode

    def is_alive(self) -> bool:
        """Whether it still runs."""
        return self._process.is_alive()

    def heartbeat(self) -> int:
        """The shared counter."""
        return int(self._counter.value)

    def kill(self) -> None:
        """SIGKILL / TerminateProcess, then reap."""
        with contextlib.suppress(Exception):
            self._process.kill()
            self._process.join(1.0)


StartMethod = Literal["spawn", "fork", "forkserver"]


def _context(method: StartMethod) -> tuple[BaseContext, Callable[..., BaseProcess]]:
    """The context and its process class.

    Typed as `BaseContext` because the fork contexts do not exist on Windows;
    the process class is returned separately since `BaseContext` does not
    declare `Process`.
    """
    if sys.platform != "win32":
        if method == "fork":
            fork = multiprocessing.get_context("fork")
            return fork, fork.Process
        if method == "forkserver":
            server = multiprocessing.get_context("forkserver")
            return server, server.Process
    elif method != "spawn":
        raise ValueError(f"start method {method!r} is not available on Windows")
    spawn = multiprocessing.get_context("spawn")
    return spawn, spawn.Process


class ProcessLauncher:
    """A `ChildLauncher` that starts `engine_child_main` in a new process."""

    def __init__(
        self,
        factory_path: str,
        transport: IpcTransport,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        start_method: StartMethod = "spawn",
    ) -> None:
        resolve_factory(factory_path)  # fail at configuration time, not in the child
        self._factory_path = factory_path
        self._transport = transport
        self._interval = heartbeat_interval_s
        self._ctx, self._process_cls = _context(start_method)

    def launch(self) -> ProcessChild:
        """Start a child process."""
        counter: _Counter = self._ctx.RawValue(ctypes.c_uint64, 0)
        process = self._process_cls(
            target=engine_child_main,
            args=(counter, self._factory_path, self._transport, self._interval),
            name="ag-engine",
            daemon=True,
        )
        process.start()
        return ProcessChild(process, counter)
