"""3.0 (phase-2 review H4, option (a)): the service-only key.

Evidence image files and the device secret are sealed with a second DPAPI key
that only the service account can unwrap; the kiosk UI process keeps the queue
data key (rule 1) and nothing else. DPAPI is a fake `win32crypt` with scopes: a
user-scope blob opens only for the account that sealed it.
"""

import ast
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from ag_shared.api import SessionMethod
from ag_shared.health import EngineStatus
from ag_shared.ids import new_uuid7
from ag_workstation.auth import CurrentSession
from ag_workstation.clock import FakeClock
from ag_workstation.consent import CaptureGate, CapturePermit
from ag_workstation.engine_api import JPEG_SOI, BurstKind, EvidenceBurst, EvidenceFrame, FrameRole
from ag_workstation.evidence import EncryptedImageFiles, EvidenceCorruptError, EvidenceStore
from ag_workstation.evidence.files import (
    LEGACY_MAGIC,
    MAGIC,
    derive_legacy_evidence_key,
)
from ag_workstation.evidence.store import IMAGES_DIR, RemovalReason
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, KeyProvider, PayloadCipher
from ag_workstation.queue.crypto import PayloadIntegrityError
from ag_workstation.queue.keys import DpapiKeyProvider, KeyUnavailableError
from ag_workstation.service import engine_factory as ef
from ag_workstation.service import service_key as sk
from ag_workstation.service.ipc_key import IPC_KEY_FILE, ipc_authkey
from ag_workstation.service.service_key import (
    SERVICE_KEY_ENTROPY,
    SERVICE_KEY_ENV,
    SERVICE_KEY_FILE,
    SERVICE_KEY_UNAVAILABLE,
    service_key_provider,
)
from ag_workstation.service.supervisor import SupervisorHealth
from ag_workstation.sync import (
    DeviceCredentials,
    SealedFileCredentialStore,
    SyncFlag,
    UnavailableCredentialStore,
)
from ag_workstation.sync.sealed_file import SealedFile
from ag_workstation.sync.wiring import (
    CREDENTIALS_FILE,
    LEGACY_CREDENTIALS_FILE,
    SERVER_URL_ENV,
    credential_store,
    service_components,
)
from argon2 import PasswordHasher
from pydantic import SecretStr

KEY = bytes(range(32))
SERVICE_KEY = bytes(range(100, 132))
T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000d301")
SECRET = "device-secret-3f9a"  # noqa: S105 - test fixture
JPEG = JPEG_SOI + b"evidence-3.0" + b"\x11" * 300
LOCAL_MACHINE = DpapiKeyProvider.CRYPTPROTECT_LOCAL_MACHINE
SOURCES = Path(__file__).resolve().parents[2] / "workstation" / "ag_workstation"


class FakeDpapi:
    """`win32crypt` for one account: machine-scope blobs open for anyone,
    user-scope blobs only for the account that sealed them."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.protected: list[tuple[bytes | None, int]] = []
        self.unwrapped: list[bytes | None] = []

    def CryptProtectData(  # noqa: N802 - pywin32 name
        self,
        data: bytes,
        description: str | None,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> bytes:
        self.protected.append((entropy, flags))
        scope = b"*machine*" if flags & LOCAL_MACHINE else self.account.encode()
        body = bytes(b ^ 0x5A for b in data)
        return b"DP" + bytes([len(scope)]) + scope + body + (entropy or b"")

    def CryptUnprotectData(  # noqa: N802 - pywin32 name
        self,
        blob: bytes,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> tuple[str, bytes]:
        self.unwrapped.append(entropy)
        tail = entropy or b""
        if not blob.startswith(b"DP") or not blob.endswith(tail):
            raise OSError("The data is invalid")
        n = blob[2]
        scope = blob[3 : 3 + n]
        if scope not in (b"*machine*", self.account.encode()):
            raise OSError("Key not valid for use in specified state")
        return ("", bytes(b ^ 0x5A for b in blob[3 + n : len(blob) - len(tail)]))


def _run_as(monkeypatch: pytest.MonkeyPatch, dpapi: FakeDpapi) -> None:
    """Make this test process look like a Windows process of `dpapi.account`."""
    windows = SimpleNamespace(platform="win32")
    monkeypatch.setattr(ef, "sys", windows)
    monkeypatch.setattr(sk, "sys", windows)
    monkeypatch.setitem(sys.modules, "win32crypt", dpapi)
    for name in (ef.DATA_KEY_ENV, SERVICE_KEY_ENV, "AG_IPC_AUTHKEY"):
        monkeypatch.delenv(name, raising=False)


class _Session:
    def current(self) -> CurrentSession:
        return CurrentSession(
            session_id=new_uuid7(),
            user_id=new_uuid7(),
            employee_id="EMP001",
            display_name="Worker",
            has_consent=True,
            preferred_language=None,
            ppe_profile_id=None,
            method=SessionMethod.EMPLOYEE_ID_PIN,
            station_assignment_id=None,
            started_at=T0 - timedelta(days=1),
            valid_until=None,
        )


class _NoBundle:
    def current_bundle(self) -> None:
        return None


class _Supervisor:
    def health(self) -> SupervisorHealth:
        return SupervisorHealth(
            status=EngineStatus.RUNNING, pid=7, restarts=0, last_error=None, consecutive_failures=0
        )


def _save(store: EvidenceStore, jpeg: bytes = JPEG) -> UUID:
    """One permitted key frame through the store's real write path."""
    burst = EvidenceBurst(
        event_id=new_uuid7(),
        kind=BurstKind.SCAN_WARNING,
        triggered_at=T0,
        trigger_ts=0.0,
        frames=(
            EvidenceFrame(
                camera_slot=1,
                captured_at=T0,
                offset_ms=0,
                seq=1,
                role=FrameRole.KEY,
                jpeg=jpeg,
            ),
        ),
        expected_frames=1,
    )
    gate = CaptureGate(sessions=_Session(), bundles=_NoBundle(), clock=FakeClock(start_utc=T0))
    permit = gate.permit(burst)
    assert isinstance(permit, CapturePermit)
    (image_id,) = store.save_burst(burst, permit=permit).image_ids
    return image_id


def _make_legacy(store: EvidenceStore, root: Path, image_id: UUID, data_keys: KeyProvider) -> None:
    """Rewrite an image file the way releases before 3.0 sealed it (data-key subkey)."""
    image = store.image(image_id)
    assert image is not None
    jpeg = store.read_for_upload(image_id).jpeg
    cipher = PayloadCipher(InMemoryKeyProvider(derive_legacy_evidence_key(data_keys)))
    nonce, sealed = cipher.encrypt(jpeg, image.associated_data())
    path = root / IMAGES_DIR / f"{image_id}.agev"
    path.write_bytes(LEGACY_MAGIC + nonce + sealed)


# -- where the key comes from ----------------------------------------------------------


def test_env_key_is_the_development_source(tmp_path: Path) -> None:
    keys = service_key_provider({SERVICE_KEY_ENV: SERVICE_KEY.hex()}, tmp_path, platform="win32")
    assert keys is not None and keys.data_key() == SERVICE_KEY
    assert not (tmp_path / SERVICE_KEY_FILE).exists()
    assert service_key_provider({}, tmp_path, platform="linux") is None
    assert service_key_provider({}, None, platform="win32") is None


@pytest.mark.parametrize("bad", ["zz", "ab" * 16, "ab" * 33])
def test_bad_env_key_is_refused(bad: str, tmp_path: Path) -> None:
    assert service_key_provider({SERVICE_KEY_ENV: bad}, tmp_path, platform="linux") is None


def test_env_key_equal_to_the_data_key_is_refused(tmp_path: Path) -> None:
    env = {SERVICE_KEY_ENV: KEY.hex(), ef.DATA_KEY_ENV: KEY.hex().upper()}
    assert service_key_provider(env, tmp_path, platform="linux") is None


def test_windows_key_is_created_in_the_service_accounts_user_scope(tmp_path: Path) -> None:
    service = FakeDpapi("svc")
    keys = service_key_provider({}, tmp_path, platform="win32", crypt_loader=lambda: service)
    assert keys is not None
    key = keys.data_key()
    assert len(key) == 32
    raw = (tmp_path / SERVICE_KEY_FILE).read_bytes()
    assert raw.startswith(sk.SERVICE_KEY_MAGIC) and key not in raw
    ((entropy, flags),) = service.protected
    assert entropy == SERVICE_KEY_ENTROPY
    assert not flags & LOCAL_MACHINE, "user scope: only the service account can unwrap it"
    again = service_key_provider({}, tmp_path, platform="win32", crypt_loader=lambda: service)
    assert again is not None and again.data_key() == key  # the engine child, same account
    kiosk = FakeDpapi("kiosk")
    as_kiosk = service_key_provider({}, tmp_path, platform="win32", crypt_loader=lambda: kiosk)
    assert as_kiosk is not None
    with pytest.raises(KeyUnavailableError):
        as_kiosk.data_key()
    assert sk.usable(as_kiosk) is None and sk.usable(None) is None


# -- key separation --------------------------------------------------------------------


def _three_keys(root: Path, dpapi: FakeDpapi) -> tuple[bytes, bytes, bytes]:
    data = DpapiKeyProvider(root / "keys" / "data.key", crypt_loader=lambda: dpapi).data_key()
    ipc = ipc_authkey({}, root, platform="win32", crypt_loader=lambda: dpapi)
    assert ipc is not None
    service = service_key_provider({}, root, platform="win32", crypt_loader=lambda: dpapi)
    assert service is not None
    return data, ipc, service.data_key()


def test_the_three_keys_differ_and_their_files_do_not_swap(tmp_path: Path) -> None:
    dpapi = FakeDpapi("svc")
    data, ipc, service = _three_keys(tmp_path / "a", dpapi)
    assert len({data, ipc, service}) == 3
    a = tmp_path / "a"
    files = {"data": a / "keys" / "data.key", "ipc": a / IPC_KEY_FILE, "svc": a / SERVICE_KEY_FILE}

    def swapped(src: str, dst: str) -> Path:
        root = tmp_path / f"{src}-as-{dst}"
        target = root / files[dst].relative_to(a)
        target.parent.mkdir(parents=True)
        shutil.copy(files[src], target)
        return root

    for src in ("data", "ipc"):
        root = swapped(src, "svc")
        keys = service_key_provider({}, root, platform="win32", crypt_loader=lambda: dpapi)
        assert keys is not None
        with pytest.raises(KeyUnavailableError):
            keys.data_key()
    root = swapped("svc", "data")
    with pytest.raises(KeyUnavailableError):
        DpapiKeyProvider(root / "keys" / "data.key", crypt_loader=lambda: dpapi).data_key()
    root = swapped("svc", "ipc")
    assert ipc_authkey({}, root, platform="win32", crypt_loader=lambda: dpapi) is None


def test_a_data_key_blob_relabelled_as_a_service_key_fails_on_entropy(tmp_path: Path) -> None:
    dpapi = FakeDpapi("svc")
    _three_keys(tmp_path, dpapi)
    blob = (tmp_path / "keys" / "data.key").read_bytes()[len(DpapiKeyProvider.MAGIC) :]
    forged = tmp_path / "forged"
    (forged / SERVICE_KEY_FILE).parent.mkdir(parents=True)
    (forged / SERVICE_KEY_FILE).write_bytes(sk.SERVICE_KEY_MAGIC + blob)
    keys = service_key_provider({}, forged, platform="win32", crypt_loader=lambda: dpapi)
    assert keys is not None
    with pytest.raises(KeyUnavailableError):
        keys.data_key()


def test_files_sealed_with_one_key_do_not_open_with_the_other(tmp_path: Path) -> None:
    data, service = InMemoryKeyProvider(KEY), InMemoryKeyProvider(SERVICE_KEY)
    ad = b"ad"
    EncryptedImageFiles(tmp_path / "e", service).write(STATION, JPEG, ad)
    assert (tmp_path / "e" / f"{STATION}.agev").read_bytes().startswith(MAGIC)
    with pytest.raises(PayloadIntegrityError):
        EncryptedImageFiles(tmp_path / "e", data).read(STATION, ad)
    assert EncryptedImageFiles(tmp_path / "e", service).read(STATION, ad) == JPEG
    creds = DeviceCredentials(STATION, SecretStr(SECRET))
    SealedFileCredentialStore(tmp_path / "c.agk", service).save(creds)
    with pytest.raises(PayloadIntegrityError):
        SealedFileCredentialStore(tmp_path / "c.agk", data).load()


# -- process roles ---------------------------------------------------------------------


def test_ui_role_cannot_open_evidence_or_the_device_secret_and_service_role_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ag_workstation.ui.app import assemble

    data = tmp_path / "data"
    env = {ef.DATA_DIR_ENV: str(data)}
    clock = FakeClock(start_utc=T0)

    # Service account: what `default_service_host` and the engine child set up.
    service = FakeDpapi("svc")
    _run_as(monkeypatch, service)
    keys = ef.key_provider_from_env(env, data)
    assert keys is not None
    queue = EventQueue.open(data / "queue.db", keys, clock)
    parts = service_components(
        env, data_dir=data, keys=keys, queue=queue, clock=clock, supervisor=_Supervisor()
    )
    assert parts.degraded == ()
    for close in parts.closers:
        close()
    service_keys = service_key_provider(env, data)
    assert service_keys is not None
    store = EvidenceStore.open(data / "evidence", service_keys, clock)
    image_id = _save(store)
    image = store.image(image_id)
    assert image is not None
    credential_store(data, keys, service_keys).save(DeviceCredentials(STATION, SecretStr(SECRET)))
    assert store.read_for_upload(image_id).jpeg == JPEG
    loaded = credential_store(data, keys, service_key_provider(env, data)).load()
    assert loaded is not None and loaded.device_secret.get_secret_value() == SECRET
    store.close()
    queue.close()
    assert SECRET.encode() not in (data / CREDENTIALS_FILE).read_bytes()

    # Kiosk account: the UI process, assembled as at start-up.
    kiosk = FakeDpapi("kiosk")
    _run_as(monkeypatch, kiosk)
    built = assemble(
        data_dir=data,
        dev=False,
        clock=clock,
        hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1),
    )
    try:
        assert built.degraded == ()  # it has the data key: queue, journal, record
    finally:
        built.close()
    assert DpapiKeyProvider.ENTROPY in kiosk.unwrapped
    assert SERVICE_KEY_ENTROPY not in kiosk.unwrapped, "the UI never touches the service key"
    ui_keys = ef.key_provider_from_env(os.environ, data)  # the key `assemble` holds
    assert ui_keys is not None
    with pytest.raises(PayloadIntegrityError):
        EncryptedImageFiles(data / "evidence" / IMAGES_DIR, ui_keys).read(
            image_id, image.associated_data()
        )
    with pytest.raises(PayloadIntegrityError):
        SealedFileCredentialStore(data / CREDENTIALS_FILE, ui_keys).load()
    # Even reading the key file does not help: DPAPI user scope of the service account.
    as_kiosk = service_key_provider(env, data)
    assert as_kiosk is not None
    with pytest.raises(KeyUnavailableError):
        as_kiosk.data_key()


SERVICE_KEY_USERS = {
    "service/service_key.py": "defines it",
    "service/engine_factory.py": "the engine child seals evidence files",
    "sync/wiring.py": "the service parent: evidence uploads and the device credentials",
}


def test_only_service_modules_use_the_service_key() -> None:
    """The UI process (ui/, scan/, auth/, record/) never imports the service key."""
    users = set()
    for path in SOURCES.rglob("*.py"):
        rel = path.relative_to(SOURCES).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("service_key"):
                users.add(rel)
            if isinstance(node, ast.alias) and node.name.endswith("service_key"):
                users.add(rel)
    assert users <= set(SERVICE_KEY_USERS) - {"service/service_key.py"}, users


# -- fail-open -------------------------------------------------------------------------


def test_service_without_its_key_still_runs_but_cannot_authenticate(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    keys = InMemoryKeyProvider(KEY)
    queue = EventQueue.open(tmp_path / "queue.db", keys, clock)
    parts = service_components(
        {SERVER_URL_ENV: "https://ag.example"},
        data_dir=tmp_path,
        keys=keys,
        queue=queue,
        clock=clock,
        supervisor=_Supervisor(),
    )
    try:
        assert parts.degraded == (SERVICE_KEY_UNAVAILABLE,)
        assert "evidence_housekeeping" not in parts.housekeeper.jobs
        assert "health_sample" in parts.housekeeper.jobs
        assert parts.sync is not None
        assert parts.sync.client.tick() == 5.0  # no network without credentials
        flags = parts.sync.client.health().flags
        assert {SyncFlag.SERVICE_KEY_UNAVAILABLE, SyncFlag.CREDENTIAL_STORE_FAILED} <= flags
        assert not (tmp_path / "evidence").exists()
    finally:
        parts.sync.stop() if parts.sync is not None else None
        for close in parts.closers:
            close()
        queue.close()


def test_unavailable_credential_store_refuses() -> None:
    store = UnavailableCredentialStore()
    with pytest.raises(KeyUnavailableError):
        store.load()
    with pytest.raises(KeyUnavailableError):
        store.save(DeviceCredentials(STATION, SecretStr(SECRET)))


def test_a_broken_service_key_counts_as_unavailable(tmp_path: Path) -> None:
    class Broken:
        def data_key(self) -> bytes:
            raise KeyUnavailableError("DPAPI refused")

    clock = FakeClock(start_utc=T0)
    keys = InMemoryKeyProvider(KEY)
    queue = EventQueue.open(tmp_path / "queue.db", keys, clock)
    parts = service_components(
        {},
        data_dir=tmp_path,
        keys=keys,
        queue=queue,
        clock=clock,
        supervisor=_Supervisor(),
        service_keys=Broken(),
    )
    for close in parts.closers:
        close()
    queue.close()
    assert parts.degraded == (SERVICE_KEY_UNAVAILABLE,)


# -- migration of files sealed before 3.0 ------------------------------------------------


def test_legacy_evidence_is_resealed_once_on_the_first_service_start(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    data_keys, service_keys = InMemoryKeyProvider(KEY), InMemoryKeyProvider(SERVICE_KEY)
    store = EvidenceStore.open(tmp_path, service_keys, clock)
    first = _save(store)
    _make_legacy(store, tmp_path, first, data_keys)
    store.close()
    path = tmp_path / IMAGES_DIR / f"{first}.agev"
    assert path.read_bytes().startswith(LEGACY_MAGIC)

    # The engine child opens without the data key's legacy role: nothing is touched.
    child = EvidenceStore.open(tmp_path, service_keys, clock)
    child.close()
    assert path.read_bytes().startswith(LEGACY_MAGIC)

    parent = EvidenceStore.open(tmp_path, service_keys, clock, legacy_keys=data_keys)
    assert path.read_bytes().startswith(MAGIC)
    assert parent.read_for_upload(first).jpeg == JPEG
    # A legacy file appearing after the migration (written by whoever holds only
    # the data key) is never read: it fails as corrupt at upload.
    planted = _save(parent, JPEG + b"x")
    _make_legacy(parent, tmp_path, planted, data_keys)
    parent.close()
    again = EvidenceStore.open(tmp_path, service_keys, clock, legacy_keys=data_keys)
    try:
        with pytest.raises(EvidenceCorruptError):
            again.read_for_upload(planted)
        assert again.read_for_upload(first).jpeg == JPEG
    finally:
        again.close()


def test_legacy_evidence_that_does_not_open_is_removed_as_corrupt(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    service_keys = InMemoryKeyProvider(SERVICE_KEY)
    store = EvidenceStore.open(tmp_path, service_keys, clock)
    image_id = _save(store)
    _make_legacy(store, tmp_path, image_id, InMemoryKeyProvider(bytes(32)))  # another station's key
    store.close()
    migrated = EvidenceStore.open(
        tmp_path, service_keys, clock, legacy_keys=InMemoryKeyProvider(KEY)
    )
    try:
        image = migrated.image(image_id)
        assert image is not None and image.removed_reason is RemovalReason.CORRUPT
        assert not (tmp_path / IMAGES_DIR / f"{image_id}.agev").exists()
        assert migrated.stats().corrupt == 1
    finally:
        migrated.close()


def test_legacy_file_without_a_legacy_key_cannot_be_resealed(tmp_path: Path) -> None:
    files = EncryptedImageFiles(tmp_path, InMemoryKeyProvider(SERVICE_KEY))
    files.write(STATION, JPEG, b"ad")
    assert not files.is_legacy(STATION) and not files.is_legacy(new_uuid7())
    with pytest.raises(KeyUnavailableError):
        files.reseal_legacy(STATION, b"ad")


def test_legacy_device_credentials_move_to_the_service_key(tmp_path: Path) -> None:
    data_keys, service_keys = InMemoryKeyProvider(KEY), InMemoryKeyProvider(SERVICE_KEY)
    legacy = tmp_path / LEGACY_CREDENTIALS_FILE
    SealedFileCredentialStore(legacy, data_keys).save(DeviceCredentials(STATION, SecretStr(SECRET)))
    loaded = credential_store(tmp_path, data_keys, service_keys).load()
    assert loaded is not None and loaded.workstation_id == STATION
    assert loaded.device_secret.get_secret_value() == SECRET
    assert not legacy.exists()
    moved = tmp_path / CREDENTIALS_FILE
    with pytest.raises(PayloadIntegrityError):
        SealedFileCredentialStore(moved, data_keys).load()
    again = SealedFileCredentialStore(moved, service_keys).load()
    assert again is not None and again.device_secret.get_secret_value() == SECRET
    # Once moved, a legacy file planted with the data key is ignored.
    SealedFileCredentialStore(legacy, data_keys).save(
        DeviceCredentials(new_uuid7(), SecretStr("planted"))
    )
    kept = credential_store(tmp_path, data_keys, service_keys).load()
    assert kept is not None and kept.workstation_id == STATION


def test_legacy_credentials_stay_when_the_new_file_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_keys, service_keys = InMemoryKeyProvider(KEY), InMemoryKeyProvider(SERVICE_KEY)
    legacy = tmp_path / LEGACY_CREDENTIALS_FILE
    SealedFileCredentialStore(legacy, data_keys).save(DeviceCredentials(STATION, SecretStr(SECRET)))

    def disk_full(self: SealedFile, plaintext: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SealedFile, "write", disk_full)
    loaded = credential_store(tmp_path, data_keys, service_keys).load()
    assert loaded is not None and loaded.device_secret.get_secret_value() == SECRET
    assert legacy.exists()  # tried again at the next load


def test_no_credentials_anywhere_means_not_enrolled(tmp_path: Path) -> None:
    store = credential_store(tmp_path, InMemoryKeyProvider(KEY), InMemoryKeyProvider(SERVICE_KEY))
    assert store.load() is None
    assert isinstance(
        credential_store(tmp_path, InMemoryKeyProvider(KEY), None), UnavailableCredentialStore
    )
