"""2.R H2: where the IPC authkey comes from (env for development, DPAPI key file on Windows)."""

from pathlib import Path

import pytest
from ag_workstation.queue.keys import DpapiKeyProvider
from ag_workstation.service.ipc_key import (
    IPC_KEY_ENTROPY,
    IPC_KEY_FILE,
    ipc_authkey,
)

KEY_HEX = "ab" * 32


class FakeWin32Crypt:
    """xor 'protection'; records the entropy each call used."""

    def __init__(self) -> None:
        self.entropies: list[bytes | None] = []
        self.fail = False

    def CryptProtectData(  # noqa: N802 - pywin32 name
        self,
        data: bytes,
        description: str | None,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> bytes:
        self.entropies.append(entropy)
        return b"DP" + bytes(b ^ 0x5A for b in data) + (entropy or b"")

    def CryptUnprotectData(  # noqa: N802 - pywin32 name
        self,
        blob: bytes,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
    ) -> tuple[str, bytes]:
        tail = entropy or b""
        if self.fail or not blob.startswith(b"DP") or not blob.endswith(tail):
            raise OSError("The data is invalid")
        body = blob[2 : len(blob) - len(tail)]
        return ("", bytes(b ^ 0x5A for b in body))


def test_env_key_wins_everywhere(tmp_path: Path) -> None:
    env = {"AG_IPC_AUTHKEY": KEY_HEX}
    assert ipc_authkey(env, tmp_path, platform="win32") == bytes.fromhex(KEY_HEX)
    assert ipc_authkey(env, None, platform="linux") == bytes.fromhex(KEY_HEX)
    assert not (tmp_path / IPC_KEY_FILE).exists()


@pytest.mark.parametrize("bad", ["zz", "ab" * 8 + "a", "ab" * 15])
def test_bad_env_key_is_refused_not_used(bad: str, tmp_path: Path) -> None:
    assert ipc_authkey({"AG_IPC_AUTHKEY": bad}, tmp_path, platform="linux") is None


def test_no_key_off_windows_without_env(tmp_path: Path) -> None:
    assert ipc_authkey({}, tmp_path, platform="linux") is None


def test_windows_key_file_is_created_once_and_shared(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    service = ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake)
    ui = ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake)
    assert service is not None and len(service) == 32
    assert ui == service  # the UI reads what the service created (or the reverse)
    raw = (tmp_path / IPC_KEY_FILE).read_bytes()
    assert service not in raw  # wrapped, never in the clear
    assert fake.entropies == [IPC_KEY_ENTROPY]


def test_ipc_key_is_not_the_data_key_and_files_do_not_swap(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    data = DpapiKeyProvider(tmp_path / "keys" / "data.key", crypt_loader=lambda: fake)
    data_key = data.data_key()
    ipc = ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake)
    assert ipc is not None and ipc != data_key
    entropies: set[bytes] = {IPC_KEY_ENTROPY, DpapiKeyProvider.ENTROPY}
    assert len(entropies) == 2
    # A data key file put at the IPC key path does not unwrap as the IPC key.
    (tmp_path / IPC_KEY_FILE).write_bytes((tmp_path / "keys" / "data.key").read_bytes())
    assert ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake) is None


def test_unusable_key_file_gives_no_key(tmp_path: Path) -> None:
    fake = FakeWin32Crypt()
    assert ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake) is not None
    fake.fail = True
    assert ipc_authkey({}, tmp_path, platform="win32", crypt_loader=lambda: fake) is None
    assert ipc_authkey({}, None, platform="win32", crypt_loader=lambda: fake) is None
