"""Run-scoped context and the run-level cost budget (§5.5 of the design doc).

Two things live here, for one reason each.

``RunContext`` is the *only* carrier for run-level facts (run id and clock, cost
budget, profile provenance, output scanner, record handle). Giving each feature
its own constructor parameter would grow every consumer's signature — and every
call site in ``cli.py`` — once per feature.

``RunBudget`` is *run* level, not provider level. The primary and the fallback
are two distinct provider instances; if each held its own budget, a run capped
at 5 USD could spend 10. ``cli.py`` builds one instance and injects it into both
lanes.

Zero IO, zero global state, and deliberately no environment reads: the run's
identity is injected so that a frozen identity reproduces a run byte for byte.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from memory_net.distiller.llm.recording import BundleRecorder


class BudgetExhausted(RuntimeError):
    """A reservation would cross a run ceiling, so the call is not made.

    Raised by ``RunBudget.reserve`` *before* the provider issues its request:
    settling after the fact cannot prevent an overspend, only report one.
    """


def utc_now() -> str:
    """The project's timestamp format, second resolution, ``Z`` suffixed."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RunIdentity:
    """The run's id and its clock — the single source for both.

    Injecting these is what makes a run reproducible: with a fixed identity the
    ``run_id``, ``generated_at`` and every hash derived from them are stable, so
    two runs of the same input compare byte for byte. A module that reads the
    clock itself silently opts out of that guarantee.
    """

    run_id: str
    now: Callable[[], str]

    @classmethod
    def system(cls) -> RunIdentity:
        """The wall-clock identity, used when nothing was injected.

        This is the *only* sanctioned default factory; it exists so consumers
        can keep their pre-injection behaviour without each re-deriving a run id
        format of its own.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return cls(run_id=f"{timestamp}-{uuid.uuid4().hex[:8]}", now=utc_now)


@dataclass
class Reservation:
    """One in-flight attempt's hold on the run budget.

    Exactly one of the three terminal methods must be called per attempt. The
    split exists because "the call failed" does not tell us whether it was
    billed, and guessing wrong in the optimistic direction lets a run overspend.
    """

    _budget: RunBudget
    _cost_upper_usd: float
    _wall_upper_s: float
    _settled: bool = False

    @property
    def is_settled(self) -> bool:
        """Whether a terminal state has already been reached.

        Lets a caller wrap an attempt in a belt-and-braces guard without that
        guard double-terminating a reservation the happy path already closed.
        """
        return self._settled

    def settle(self, *, usage: dict[str, int], wall_s: float) -> None:
        """Success: charge the real usage and refund the rest of the hold."""
        self._finish(
            charge_usd=self._budget.cost_of(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            ),
            wall_s=wall_s,
        )

    def settle_unknown(self, *, wall_s: float) -> None:
        """Timeout / dropped connection / unknown outcome: keep the full hold.

        The request may already have been executed and billed upstream. Refunding
        the hold would overstate the remaining budget, which is precisely the
        direction that causes an overspend, so this is the conservative default.
        """
        self._finish(charge_usd=self._cost_upper_usd, wall_s=wall_s)

    def release(self, *, wall_s: float) -> None:
        """Provably not executed: refund the whole hold.

        Only for failures that happened *before* the request went out (missing
        key, invalid arguments) or where the service explicitly said it did not
        run. A 5xx with no usage body does not qualify: the call may have run to
        completion and failed on the way back.
        """
        self._finish(charge_usd=0.0, wall_s=wall_s)

    def _finish(self, *, charge_usd: float, wall_s: float) -> None:
        if self._settled:
            raise RuntimeError("reservation was already terminated")
        self._settled = True
        self._budget._close(
            reservation=self,
            charge_usd=charge_usd,
            wall_s=wall_s,
        )


@dataclass
class RunBudget:
    """The run's shared spend ceiling, enforced by reserving before each call.

    Mutable and not thread shared. Pricing lives here rather than in each
    provider so there is one conversion from tokens to dollars; callers only
    supply token upper bounds.
    """

    input_per_mtok_usd: float
    output_per_mtok_usd: float
    max_usd: float | None = None
    max_attempts: int | None = None
    max_wall_clock_s: float | None = None

    spent_usd: float = 0.0
    attempts: int = 0
    wall_clock_s: float = 0.0
    _outstanding: list[Reservation] = field(default_factory=list)

    def cost_of(self, *, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000) * self.input_per_mtok_usd + (
            output_tokens / 1_000_000
        ) * self.output_per_mtok_usd

    def reserve(
        self,
        *,
        input_tokens_upper: int,
        output_tokens_upper: int,
        timeout_s_upper: float,
    ) -> Reservation:
        """Hold budget for one attempt, or refuse to let it start.

        Every dimension is checked against its *upper* bound, wall clock
        included: starting a 120s request with 1s of allowance left cannot
        possibly stay inside the ceiling, so it is refused rather than begun.
        """
        cost_upper = self.cost_of(
            input_tokens=input_tokens_upper, output_tokens=output_tokens_upper
        )
        if self.max_attempts is not None:
            # In-flight reservations count: they are attempts that have started.
            in_flight = self.attempts + len(self._outstanding)
            if in_flight >= self.max_attempts:
                raise BudgetExhausted(
                    f"run budget exhausted: attempts {in_flight}/{self.max_attempts}"
                )
        if self.max_usd is not None:
            projected = self.spent_usd + self.reserved_usd + cost_upper
            if projected > self.max_usd:
                raise BudgetExhausted(
                    f"run budget exhausted: cost {projected:.6f}"
                    f"/{self.max_usd:.6f} USD"
                )
        if self.max_wall_clock_s is not None:
            projected_s = (
                self.wall_clock_s + self.reserved_wall_clock_s + timeout_s_upper
            )
            if projected_s > self.max_wall_clock_s:
                raise BudgetExhausted(
                    f"run budget exhausted: wall clock {projected_s:.3f}"
                    f"/{self.max_wall_clock_s:.3f}s"
                )
        reservation = Reservation(
            _budget=self,
            _cost_upper_usd=cost_upper,
            _wall_upper_s=timeout_s_upper,
        )
        self._outstanding.append(reservation)
        return reservation

    @property
    def reserved_usd(self) -> float:
        return sum(item._cost_upper_usd for item in self._outstanding)

    @property
    def reserved_wall_clock_s(self) -> float:
        return sum(item._wall_upper_s for item in self._outstanding)

    def assert_all_settled(self) -> None:
        """Fail loudly on a leaked reservation; call from a ``finally`` block.

        Deliberately not hooked to ``__del__``: destructor timing is not
        deterministic, so it cannot be relied on to detect this.
        """
        if self._outstanding:
            raise RuntimeError(
                f"run budget has {len(self._outstanding)} unsettled reservation(s)"
            )

    def snapshot(self) -> dict[str, Any]:
        """Stable, JSON-serialisable view for metrics."""
        return {
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "max_usd": self.max_usd,
            "max_wall_clock_s": self.max_wall_clock_s,
            "reserved_usd": self.reserved_usd,
            "reserved_wall_clock_s": self.reserved_wall_clock_s,
            "spent_usd": self.spent_usd,
            "wall_clock_s": self.wall_clock_s,
        }

    def _close(
        self,
        *,
        reservation: Reservation,
        charge_usd: float,
        wall_s: float,
    ) -> None:
        """Drop the hold and commit the attempt's real cost.

        An overspend (real usage above the reserved bound) is recorded, never
        rolled back — the money is already gone. It simply makes the next
        ``reserve`` fail.
        """
        self._outstanding.remove(reservation)
        self.spent_usd += charge_usd
        self.wall_clock_s += wall_s
        self.attempts += 1


def prompt_tokens_upper(*parts: str) -> int:
    """Conservative token upper bound for prompt text: 4 characters per token.

    One shared estimator so the budget's view of an input's size cannot drift
    from any other caller's.
    """
    return math.ceil(sum(len(part) for part in parts) / 4)


@dataclass(frozen=True)
class RunContext:
    """Everything the CLI computes once per run and injects into the consumers.

    Every field is optional, and ``None`` means "this feature does not apply to
    this run": a consumer then keeps the behaviour it had before injection
    existed, which is what lets the wiring land incrementally.
    """

    identity: RunIdentity | None = None
    budget: RunBudget | None = None
    # Narrowed as the owning modules land (profile provenance, output scanner).
    profile: object | None = None
    scanner: object | None = None
    recorder: BundleRecorder | None = None


class RunContextBuilder:
    """Mutable assembly for exactly one ``RunContext``; used only by ``cli.py``.

    ``RunContext`` is frozen, so it cannot be filled in field by field as the
    CLI computes things. The builder collects, then ``freeze()`` produces the
    single immutable instance handed to every consumer. Both the setters and
    ``freeze()`` refuse to run twice-over, because two consumers holding
    different versions of the context is a defect nothing at runtime would
    notice.
    """

    def __init__(self) -> None:
        self._fields: dict[str, Any] = {}
        self._frozen = False

    def set_identity(self, identity: RunIdentity | None) -> Self:
        return self._set("identity", identity)

    def set_budget(self, budget: RunBudget | None) -> Self:
        return self._set("budget", budget)

    def set_profile(self, profile: object | None) -> Self:
        return self._set("profile", profile)

    def set_scanner(self, scanner: object | None) -> Self:
        return self._set("scanner", scanner)

    def set_recorder(self, recorder: BundleRecorder | None) -> Self:
        return self._set("recorder", recorder)

    def freeze(self) -> RunContext:
        if self._frozen:
            raise RuntimeError("run context builder is already frozen")
        self._frozen = True
        return RunContext(**self._fields)

    def _set(self, name: str, value: object) -> Self:
        if self._frozen:
            raise RuntimeError(f"run context builder is frozen; cannot set {name}")
        self._fields[name] = value
        return self
