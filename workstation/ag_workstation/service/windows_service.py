"""Windows service wrapper (pywin32). Windows only; imported lazily.

Thin by design: start the `ServiceHost` from a factory, block until the SCM
asks to stop, stop the host. All logic lives in the portable host. Nothing
here imports pywin32 at module import time, so this file loads (and type
checks) on Linux; `make_service_class` imports it when called.

Install, start and recovery options (auto-restart of the service itself) are
set by the installer (PLAN 6.x): `python -m ag_workstation.service.windows_service install`.
"""

import importlib
from collections.abc import Callable
from typing import Any, Final

from ag_workstation.service.host import ServiceHost, default_service_host

SERVICE_NAME: Final = "AttentionGuard"
DISPLAY_NAME: Final = "AttentionGuard monitoring service"
DESCRIPTION: Final = "Runs and supervises the AttentionGuard detection engine."

HostFactory = Callable[[], ServiceHost]


def make_service_class(host_factory: HostFactory) -> type:
    """Build the `win32serviceutil.ServiceFramework` subclass for `host_factory`."""
    # pywin32 has no type stubs; its objects are typed Any on purpose.
    win32serviceutil: Any = importlib.import_module("win32serviceutil")
    win32service: Any = importlib.import_module("win32service")
    win32event: Any = importlib.import_module("win32event")
    servicemanager: Any = importlib.import_module("servicemanager")

    class AttentionGuardService(win32serviceutil.ServiceFramework):  # type: ignore[misc]  # base is Any (no pywin32 stubs)
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.host: ServiceHost | None = None

        def SvcStop(self) -> None:  # noqa: N802 - pywin32 callback name
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self) -> None:  # noqa: N802 - pywin32 callback name
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting")
            host = self.host = host_factory()
            host.start()
            try:
                win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            finally:
                host.stop()
                servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")

    return AttentionGuardService


def main(host_factory: HostFactory = default_service_host, argv: list[str] | None = None) -> None:
    """Command-line entry (install / start / stop / remove / debug)."""
    win32serviceutil: Any = importlib.import_module("win32serviceutil")
    win32serviceutil.HandleCommandLine(make_service_class(host_factory), argv=argv)


if __name__ == "__main__":  # pragma: no cover - Windows service command line
    main()
