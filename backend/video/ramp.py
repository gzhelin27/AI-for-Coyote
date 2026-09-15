"""Transport-independent strength proposals with acknowledgement-based cadence."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RampProposal:
    requested: int
    target: int
    strength: int
    confirmed: int
    ramping: bool
    revision: int
    _owner: object


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')


def _time(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('now_s must be a finite monotonic time')


class RampPolicy:
    """One channel's acknowledged ramp state; callers serialize pending output.

    ``propose`` never mutates state. Submit its ``strength`` through safety,
    then confirm with the actual acknowledged strength and acknowledgement
    time. Failed output must not call ``confirm``. Cancel after retiring an
    output generation; a proposal from that generation cannot be confirmed.
    Safety caps and playback authorization must be rechecked by the caller.
    If a changed target already equals confirmed output, no command is needed;
    the caller cancels the previous target state. Unchanged targets retain it.
    Proposal.ramping identifies the ramp path; confirmation ends that path
    only when the actual result reaches its capped target.
    """

    def __init__(self) -> None:
        self._owner = object()
        self._revision = 0
        self._requested: int | None = None
        self._target = 0
        self._ramping = False
        self._last_increase_s: float | None = None

    def propose(self, *, requested: int, confirmed: int, cap: int,
                max_step: int, now_s: float) -> RampProposal | None:
        for name, value in (('requested', requested), ('confirmed', confirmed), ('cap', cap)):
            _integer(value, name)
        _integer(max_step, 'max_step', 1)
        _time(now_s)
        target = min(requested, cap)
        difference = target - confirmed
        if difference == 0:
            return None
        active = self._ramping and confirmed < self._target
        if difference < 0:
            strength, ramping = target, False
        elif active and (requested == self._requested or difference > max_step):
            if self._last_increase_s is not None and now_s < self._last_increase_s + 2:
                return None
            strength = confirmed + 1
            ramping = True
        else:
            strength = min(target, confirmed + max_step)
            ramping = strength < target
        return RampProposal(requested, target, strength, confirmed, ramping,
                            self._revision, self._owner)

    def confirm(self, proposal: RampProposal, *, actual: int, now_s: float) -> None:
        _integer(actual, 'actual')
        _time(now_s)
        if proposal._owner is not self._owner or proposal.revision != self._revision:
            raise ValueError('stale or foreign ramp proposal')
        if actual > proposal.strength:
            raise ValueError('acknowledged strength exceeds proposal')
        if self._last_increase_s is not None and now_s < self._last_increase_s:
            raise ValueError('acknowledgement time moved backwards')
        self._requested = proposal.requested
        self._target = proposal.target
        self._ramping = proposal.ramping and actual < proposal.target
        if actual > proposal.confirmed:
            self._last_increase_s = now_s
        self._revision += 1

    def cancel(self) -> None:
        self._revision += 1
        self._requested = None
        self._target = 0
        self._ramping = False
        self._last_increase_s = None
