"""The pywin32 service wrapper against fake win32 modules (registration itself is manual)."""

import sys
import threading
import types
from typing import Any

import pytest
from ag_workstation.service import windows_service


class FakeFramework:
    def __init__(self, args: Any) -> None:
        self.args = args
        self.statuses: list[int] = []

    def ReportServiceStatus(self, status: int) -> None:  # noqa: N802 - pywin32 name
        self.statuses.append(status)


class FakeHost:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


@pytest.fixture
def win32(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    handled: dict[str, Any] = {}
    logs: list[str] = []
    modules = {
        "win32serviceutil": types.SimpleNamespace(
            ServiceFramework=FakeFramework,
            HandleCommandLine=lambda cls, argv=None: handled.update(cls=cls, argv=argv),
        ),
        "win32service": types.SimpleNamespace(SERVICE_STOP_PENDING=3),
        "win32event": types.SimpleNamespace(
            CreateEvent=lambda *a: threading.Event(),
            SetEvent=lambda e: e.set(),
            WaitForSingleObject=lambda e, timeout: e.wait(5.0),
            INFINITE=-1,
        ),
        "servicemanager": types.SimpleNamespace(LogInfoMsg=logs.append),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return {"handled": handled, "logs": logs}


def test_service_starts_host_and_stops_it_on_scm_stop(win32: dict[str, Any]) -> None:
    host = FakeHost()
    cls = windows_service.make_service_class(lambda: host)  # type: ignore[arg-type,return-value]  # duck-typed host
    assert cls._svc_name_ == "AttentionGuard"  # type: ignore[attr-defined]  # pywin32 attribute
    svc: Any = cls(["AttentionGuard"])
    runner = threading.Thread(target=svc.SvcDoRun)
    runner.start()
    for _ in range(200):
        if host.events:
            break
        threading.Event().wait(0.005)
    svc.SvcStop()
    runner.join(2.0)
    assert host.events == ["start", "stop"]
    assert svc.statuses == [3]
    assert len(win32["logs"]) == 2


def test_main_hands_class_to_command_line(win32: dict[str, Any]) -> None:
    windows_service.main(FakeHost, ["prog", "install"])  # type: ignore[arg-type]  # duck-typed host
    assert win32["handled"]["argv"] == ["prog", "install"]
    assert win32["handled"]["cls"]._svc_display_name_ == windows_service.DISPLAY_NAME


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="real pywin32 needs Windows")
def test_real_pywin32_class_builds() -> None:  # pragma: no cover - manual Windows check
    cls = windows_service.make_service_class(lambda: None)  # type: ignore[arg-type,return-value]  # not run
    assert cls.__name__ == "AttentionGuardService"
