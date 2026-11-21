"""Manual-check mode: the real engine on a local camera, inside the UI process.

`python -m ag_workstation.ui.app --dev --camera 0 --models models` runs the
same parts as a real station (the engine child's `build_station` behind an
`IpcServer`, the UI talking to it through `IpcClient`s), but in one process,
with a per-run key, a temporary data folder and the dev roster (DEV001/1234).
No Windows service and no server are needed. For manual checks only: the
installed station keeps the engine in the service's child process.
"""

import contextlib
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ag_workstation.clock import Clock
from ag_workstation.engine.runtime import FeedFactory, ProviderFactory
from ag_workstation.landmarks.mediapipe_provider import MODEL_DIR_ENV
from ag_workstation.queue import InMemoryKeyProvider, KeyProvider
from ag_workstation.service import IpcClient, PipeTransport, default_address
from ag_workstation.service.engine_factory import DATA_DIR_ENV, build_station
from ag_workstation.service.ipc_server import IpcServer
from ag_workstation.service.station import StationChild

logger = logging.getLogger(__name__)

CONTROL_TIMEOUT_S = 1.0


@dataclass
class LocalStation:
    """The in-process engine side and the two clients the UI uses."""

    child: StationChild
    server: IpcServer
    ipc: IpcClient
    control: IpcClient

    def close(self) -> None:
        """Stop serving and stop the engine. Never raises."""
        with contextlib.suppress(Exception):
            self.server.stop()
        with contextlib.suppress(Exception):
            self.child.close()


def start_local_station(
    *,
    data_dir: Path,
    models: Path | None,
    keys: KeyProvider,
    clock: Clock,
    feed_factory: FeedFactory | None = None,
    provider_factory: ProviderFactory | None = None,
) -> LocalStation:
    """Start the real engine side in this process and return clients for it.

    The engine starts when the UI pushes the station config from the dev
    bundle (its camera is the one given on the command line). A missing model
    or camera gives Unknown, as on a real station."""
    env: Mapping[str, str] = {
        DATA_DIR_ENV: str(data_dir),
        **({MODEL_DIR_ENV: str(models.resolve())} if models is not None else {}),
    }
    child = build_station(
        None,
        env=env,
        clock=clock,
        keys=keys,
        # Manual-check mode only: the engine runs in this process, so it gets a
        # per-run service key here (the installed UI never holds one, 3.0).
        service_keys=InMemoryKeyProvider(),
        feed_factory=feed_factory,
        provider_factory=provider_factory,
    )
    transport = PipeTransport(
        default_address(f"devcam-{os.getpid()}-{secrets.token_hex(4)}"), secrets.token_bytes(32)
    )
    server = IpcServer(transport, child, control=child)
    server.start()
    logger.info("local engine serving on %s", transport.address)
    return LocalStation(
        child=child,
        server=server,
        ipc=IpcClient(transport, clock),
        control=IpcClient(transport, clock, timeout_s=CONTROL_TIMEOUT_S),
    )


__all__ = ["LocalStation", "start_local_station"]
