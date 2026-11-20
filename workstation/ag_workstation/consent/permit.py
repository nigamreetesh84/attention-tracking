"""The capability that the evidence store demands before it writes a frame.

`EvidenceStore.save_burst` refuses to run without a `CapturePermit`, and only
`CaptureGate` mints one (through `_issue`, which no other module may call: a
test over the sources checks that). A permit is bound to one burst object,
can be redeemed once, cannot be copied or pickled, and carries a re-check
callback so the store asks the gate again before every frame it writes.
"""

import threading
from collections.abc import Callable
from typing import Final, NoReturn, final

from ag_workstation.consent.decision import GateDecision, GateReason
from ag_workstation.engine_api import EvidenceBurst

_MINT: Final = object()


class PermitError(PermissionError):
    """A frame write was attempted without a valid capture permit."""


@final
class CapturePermit:
    """Permission to write the frames of one burst, if the gate stays open."""

    __slots__ = ("_burst", "_lock", "_recheck", "_redeemed", "decision")

    def __init__(
        self,
        key: object,
        burst: EvidenceBurst,
        decision: GateDecision,
        recheck: Callable[[], GateDecision],
    ) -> None:
        """Not for callers: only the capture gate issues permits."""
        if key is not _MINT:
            raise PermitError("capture permits are issued only by the capture gate")
        if not decision.open:
            raise PermitError("a closed gate cannot issue a permit")
        self._burst = burst
        self._recheck = recheck
        self._redeemed = False
        self._lock = threading.Lock()
        self.decision = decision

    def redeem(self, burst: EvidenceBurst) -> None:
        """Use the permit for `burst`; raises `PermitError` if it is not that burst's or used."""
        with self._lock:
            if burst is not self._burst:
                raise PermitError("the capture permit was issued for another burst")
            if self._redeemed:
                raise PermitError("the capture permit was already used")
            self._redeemed = True

    def recheck(self) -> GateDecision:
        """Ask the gate again; any failure is a closed decision."""
        try:
            decision = self._recheck()
        except Exception:
            return GateDecision(GateReason.ERROR, None)
        if not isinstance(decision, GateDecision):
            return GateDecision(GateReason.ERROR, None)
        return decision

    def _refuse_copy(self, *_: object) -> NoReturn:
        raise TypeError("a capture permit cannot be copied")

    __copy__ = _refuse_copy
    __deepcopy__ = _refuse_copy
    __reduce_ex__ = _refuse_copy


def _issue(
    burst: EvidenceBurst, decision: GateDecision, recheck: Callable[[], GateDecision]
) -> CapturePermit:
    """Mint a permit. Only `ag_workstation.consent.gate` calls this."""
    return CapturePermit(_MINT, burst, decision, recheck)
