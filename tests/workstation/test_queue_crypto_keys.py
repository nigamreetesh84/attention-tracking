"""Queue payload encryption and data-key providers (DPAPI is mocked)."""

import os
import stat
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest
from ag_workstation.queue import (
    DATA_KEY_BYTES,
    DpapiKeyProvider,
    InMemoryKeyProvider,
    KeyUnavailableError,
    PayloadCipher,
    PayloadIntegrityError,
)
from ag_workstation.queue.keys import Win32Crypt

KEY = bytes(range(32))


def test_round_trip() -> None:
    cipher = PayloadCipher(InMemoryKeyProvider(KEY))
    nonce, ct = cipher.encrypt(b"hello", b"ad")
    assert len(nonce) == 12
    assert b"hello" not in ct
    assert cipher.decrypt(nonce, ct, b"ad") == b"hello"


def test_fresh_nonce_each_time() -> None:
    cipher = PayloadCipher(InMemoryKeyProvider(KEY))
    a = cipher.encrypt(b"same", b"ad")
    b = cipher.encrypt(b"same", b"ad")
    assert a[0] != b[0]
    assert a[1] != b[1]


@pytest.mark.parametrize("what", ["ciphertext", "nonce", "ad"])
def test_tamper_detected(what: str) -> None:
    cipher = PayloadCipher(InMemoryKeyProvider(KEY))
    nonce, ct = cipher.encrypt(b"payload", b"ad")
    if what == "ciphertext":
        ct = bytes([ct[0] ^ 1]) + ct[1:]
    elif what == "nonce":
        nonce = bytes([nonce[0] ^ 1]) + nonce[1:]
    ad = b"other" if what == "ad" else b"ad"
    with pytest.raises(PayloadIntegrityError, match="integrity"):
        cipher.decrypt(nonce, ct, ad)


def test_wrong_key_detected() -> None:
    nonce, ct = PayloadCipher(InMemoryKeyProvider(KEY)).encrypt(b"x", b"ad")
    other = PayloadCipher(InMemoryKeyProvider(bytes(32)))
    with pytest.raises(PayloadIntegrityError):
        other.decrypt(nonce, ct, b"ad")


def test_bad_nonce_length_is_integrity_error() -> None:
    cipher = PayloadCipher(InMemoryKeyProvider(KEY))
    _, ct = cipher.encrypt(b"x", b"ad")
    with pytest.raises(PayloadIntegrityError):
        cipher.decrypt(b"short", ct, b"ad")


def test_in_memory_provider() -> None:
    generated = InMemoryKeyProvider()
    assert len(generated.data_key()) == DATA_KEY_BYTES
    assert generated.data_key() == generated.data_key()
    assert InMemoryKeyProvider(KEY).data_key() == KEY
    with pytest.raises(ValueError, match="32 bytes"):
        InMemoryKeyProvider(b"short")


class FakeWin32Crypt:
    """Stands in for pywin32's `win32crypt`: 'protects' by xor and records calls."""

    def __init__(self) -> None:
        self.protect_calls: list[tuple[bytes, int]] = []
        self.fail_unprotect = False

    def CryptProtectData(  # noqa: N802 - pywin32 name
        self,
        data: bytes,
        description: str | None,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> bytes:
        assert entropy
        self.protect_calls.append((entropy, flags))
        return b"DP" + bytes(b ^ 0x5A for b in data)

    def CryptUnprotectData(  # noqa: N802 - pywin32 name
        self,
        blob: bytes,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> tuple[str, bytes]:
        if self.fail_unprotect or not blob.startswith(b"DP"):
            raise OSError("The data is invalid")
        return ("", bytes(b ^ 0x5A for b in blob[2:]))


def _provider(
    path: Path,
    fake: FakeWin32Crypt,
    *,
    machine_scope: bool = True,
    randbytes: Callable[[int], bytes] = os.urandom,
) -> DpapiKeyProvider:
    return DpapiKeyProvider(
        path, crypt_loader=lambda: fake, machine_scope=machine_scope, randbytes=randbytes
    )


def test_dpapi_creates_and_reloads_key(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    path = tmp_path / "keys" / "queue.key"
    key = _provider(path, fake, randbytes=lambda n: b"\x07" * n).data_key()
    assert key == b"\x07" * 32
    raw = path.read_bytes()
    assert key not in raw  # only the DPAPI blob is stored
    _entropy, flags = fake.protect_calls[0]
    assert flags & DpapiKeyProvider.CRYPTPROTECT_UI_FORBIDDEN
    assert flags & DpapiKeyProvider.CRYPTPROTECT_LOCAL_MACHINE
    # A second provider (another process) unwraps the same key.
    assert _provider(path, fake).data_key() == key
    assert list(path.parent.iterdir()) == [path]  # temp file cleaned up
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dpapi_user_scope(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    _provider(tmp_path / "k", fake, machine_scope=False).data_key()
    assert not fake.protect_calls[0][1] & DpapiKeyProvider.CRYPTPROTECT_LOCAL_MACHINE


def test_dpapi_caches_key(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    provider = _provider(tmp_path / "k", fake)
    first = provider.data_key()
    (tmp_path / "k").unlink()
    assert provider.data_key() == first


def test_dpapi_lost_race_uses_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two processes creating the key at once must end up with the same key."""
    fake = FakeWin32Crypt()
    path = tmp_path / "k"
    winner = _provider(path, fake, randbytes=lambda n: b"\x01" * n)
    loser = _provider(path, fake, randbytes=lambda n: b"\x02" * n)
    real_exists = Path.exists
    calls = {"n": 0}

    def exists_once_false(self: Path) -> bool:
        # The loser looks for the file just before the winner writes it.
        if self == path and calls["n"] == 0:
            calls["n"] += 1
            winner.data_key()
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", exists_once_false)
    assert loser.data_key() == b"\x01" * 32
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k"]


def test_dpapi_unprotect_failure_is_clear(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    path = tmp_path / "k"
    _provider(path, fake).data_key()
    fake.fail_unprotect = True
    with pytest.raises(KeyUnavailableError, match="unwrap"):
        _provider(path, fake).data_key()


def test_dpapi_rejects_bad_file(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    path = tmp_path / "k"
    path.write_bytes(b"garbage")
    with pytest.raises(KeyUnavailableError, match="not a queue key file"):
        _provider(path, fake).data_key()


def test_dpapi_rejects_wrong_key_length(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    path = tmp_path / "k"
    path.write_bytes(
        DpapiKeyProvider.MAGIC + fake.CryptProtectData(b"x" * 5, None, b"e", None, None, 0)
    )
    with pytest.raises(KeyUnavailableError, match="length"):
        _provider(path, fake).data_key()


def test_dpapi_missing_module(tmp_path: Path) -> None:
    def loader() -> Win32Crypt:
        raise ImportError("No module named 'win32crypt'")

    provider = DpapiKeyProvider(tmp_path / "k", crypt_loader=loader)
    with pytest.raises(KeyUnavailableError, match="DPAPI"):
        provider.data_key()


def test_dpapi_default_loader_imports_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWin32Crypt()
    module = types.ModuleType("win32crypt")
    module.CryptProtectData = fake.CryptProtectData  # type: ignore[attr-defined]  # fake module
    module.CryptUnprotectData = fake.CryptUnprotectData  # type: ignore[attr-defined]  # fake module
    monkeypatch.setitem(sys.modules, "win32crypt", module)
    key = DpapiKeyProvider(tmp_path / "k").data_key()
    assert len(key) == 32


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI needs Windows")
def test_real_dpapi_round_trip(tmp_path: Path) -> None:  # pragma: no cover - manual Windows check
    key = DpapiKeyProvider(tmp_path / "k").data_key()
    assert DpapiKeyProvider(tmp_path / "k").data_key() == key
    assert os.path.getsize(tmp_path / "k") > 32
