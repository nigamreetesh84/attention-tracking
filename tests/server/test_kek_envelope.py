"""KEK envelope encryption for sensitive columns (task 3.4; reused by 3.7)."""

from __future__ import annotations

import pytest
from ag_server.crypto import (
    ENVELOPE_MAGIC,
    EnvelopeError,
    KeyRing,
    derive_kek,
    derive_subkey,
    open_sealed,
    seal,
)

SECRET_A = "kkkkkkkkZx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9"  # noqa: S105 - test KEK
SECRET_B = "bbbbbbbbQw3Qw3Qw3Qw3Qw3Qw3Qw3Qw3Qw3Qw3"  # noqa: S105 - test KEK
AAD = b"user.totp_secret_encrypted:0001"


def test_round_trip_and_no_plaintext_in_blob() -> None:
    ring = KeyRing.from_secrets(SECRET_A)
    plain = b"JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    blob = seal(ring, plain, AAD)
    assert blob.startswith(ENVELOPE_MAGIC)
    assert plain not in blob
    assert open_sealed(ring, blob, AAD) == plain


def test_each_seal_uses_a_fresh_data_key() -> None:
    ring = KeyRing.from_secrets(SECRET_A)
    assert seal(ring, b"x", AAD) != seal(ring, b"x", AAD)


def test_associated_data_binds_the_blob_to_its_row() -> None:
    ring = KeyRing.from_secrets(SECRET_A)
    blob = seal(ring, b"secret", AAD)
    with pytest.raises(EnvelopeError):
        open_sealed(ring, blob, b"user.totp_secret_encrypted:0002")


@pytest.mark.parametrize("index", [0, 5, 20, 40, -1])
def test_tampering_is_detected(index: int) -> None:
    ring = KeyRing.from_secrets(SECRET_A)
    blob = bytearray(seal(ring, b"secret", AAD))
    blob[index] ^= 0x01
    with pytest.raises(EnvelopeError):
        open_sealed(ring, bytes(blob), AAD)


def test_truncated_or_foreign_blob_is_rejected() -> None:
    ring = KeyRing.from_secrets(SECRET_A)
    with pytest.raises(EnvelopeError):
        open_sealed(ring, b"AGK1short", AAD)
    with pytest.raises(EnvelopeError):
        open_sealed(ring, b"not an envelope at all, much longer than the header" * 2, AAD)


def test_unknown_kek_is_refused_and_previous_kek_still_opens() -> None:
    old = KeyRing.from_secrets(SECRET_A)
    blob = seal(old, b"secret", AAD)
    with pytest.raises(EnvelopeError):
        open_sealed(KeyRing.from_secrets(SECRET_B), blob, AAD)
    rotated = KeyRing.from_secrets(SECRET_B, previous=[SECRET_A])
    assert open_sealed(rotated, blob, AAD) == b"secret"
    assert rotated.needs_rewrap(blob)
    assert not rotated.needs_rewrap(seal(rotated, b"secret", AAD))


def test_kek_derivation_is_deterministic_and_ids_differ() -> None:
    a1, a2, b = derive_kek(SECRET_A), derive_kek(SECRET_A), derive_kek(SECRET_B)
    assert a1 == a2
    assert len(a1.key) == 32
    assert len(a1.kek_id) == 8
    assert a1.kek_id != b.kek_id
    # The id is not the key, nor a prefix of it.
    assert a1.kek_id not in a1.key


def test_subkeys_are_separated_by_label() -> None:
    csrf = derive_subkey(SECRET_A, "csrf")
    assert len(csrf) == 32
    assert csrf != derive_subkey(SECRET_A, "other")
    assert csrf != derive_kek(SECRET_A).key
    with pytest.raises(ValueError):
        derive_subkey(SECRET_A, "")


def test_kek_repr_hides_key_material() -> None:
    kek = derive_kek(SECRET_A)
    assert kek.key.hex() not in repr(kek)
    assert SECRET_A not in repr(KeyRing.from_secrets(SECRET_A))
