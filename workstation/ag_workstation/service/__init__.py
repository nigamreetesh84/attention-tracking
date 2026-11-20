"""Service host and IPC (PLAN 2.3).

- `EngineSupervisor`: runs the engine as a child process and restarts it.
- `IpcServer` / `IpcClient` / `EventSubscriber`: state queries and events over
  a named pipe (Windows) or Unix socket, JSON messages, 150 ms client deadline.
- `HealthReporter` / `Housekeeper`: health snapshot and periodic purges.
- `ServiceHost`: what the Windows service (`windows_service`, lazy) runs.
"""

from ag_workstation.service.child import ProcessChild, ProcessLauncher, engine_child_main
from ag_workstation.service.health import (
    HealthReporter,
    HealthSnapshot,
    Housekeeper,
    disk_free_mb_hook,
    standard_housekeeping,
)
from ag_workstation.service.host import QueueLocalAlertSink, ServiceHost
from ag_workstation.service.ipc_client import (
    CallResult,
    CallStatus,
    EventSubscriber,
    HealthResult,
    IpcClient,
    StateResult,
)
from ag_workstation.service.ipc_server import EventPublisher, IpcServer, StateAndHealth
from ag_workstation.service.supervisor import (
    RESTART_DEADLINE_S,
    ChildLauncher,
    ChildProcess,
    EngineSupervisor,
    FailureReason,
    SupervisorConfig,
    SupervisorHealth,
)
from ag_workstation.service.transport import (
    IpcConnection,
    IpcListener,
    IpcTransport,
    PipeTransport,
    default_address,
)

__all__ = [
    "RESTART_DEADLINE_S",
    "CallResult",
    "CallStatus",
    "ChildLauncher",
    "ChildProcess",
    "EngineSupervisor",
    "EventPublisher",
    "EventSubscriber",
    "FailureReason",
    "HealthReporter",
    "HealthResult",
    "HealthSnapshot",
    "Housekeeper",
    "IpcClient",
    "IpcConnection",
    "IpcListener",
    "IpcServer",
    "IpcTransport",
    "PipeTransport",
    "ProcessChild",
    "ProcessLauncher",
    "QueueLocalAlertSink",
    "ServiceHost",
    "StateAndHealth",
    "StateResult",
    "SupervisorConfig",
    "SupervisorHealth",
    "default_address",
    "disk_free_mb_hook",
    "engine_child_main",
    "standard_housekeeping",
]
