"""Run context and run-level budget value objects (§5.5).

The budget tests are written against the one property that makes the object
worth existing: a run's ceiling is enforced *before* a call is made, and one
shared instance means the primary and fallback lanes cannot each spend it.
"""

import dataclasses

import pytest

from memory_net.distiller.runctx import (
    BudgetExhausted,
    RunBudget,
    RunContext,
    RunContextBuilder,
    RunIdentity,
    prompt_tokens_upper,
)


def _budget(**overrides: object) -> RunBudget:
    kwargs: dict[str, object] = {
        "input_per_mtok_usd": 1.0,
        "output_per_mtok_usd": 10.0,
        "max_usd": 1.0,
    }
    kwargs.update(overrides)
    return RunBudget(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# RunIdentity / RunContext / RunContextBuilder
# --------------------------------------------------------------------------


def test_run_context_is_frozen() -> None:
    """Consumers hold the same object; a mutable one could drift under them."""
    context = RunContext()

    assert dataclasses.is_dataclass(RunContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.identity = RunIdentity(run_id="r", now=lambda: "t")  # type: ignore[misc]


def test_an_unset_context_is_all_none() -> None:
    """Every field is optional so a partly-wired CLI can still build one."""
    context = RunContext()

    assert (context.identity, context.budget, context.profile) == (None, None, None)
    assert (context.scanner, context.recorder) == (None, None)


def test_identity_supplies_the_only_run_id_and_clock() -> None:
    identity = RunIdentity(run_id="run-fixed", now=lambda: "2026-01-01T00:00:00Z")

    assert identity.run_id == "run-fixed"
    assert identity.now() == "2026-01-01T00:00:00Z"


def test_system_identity_produces_a_distinct_run_id_and_a_utc_timestamp() -> None:
    first = RunIdentity.system()
    second = RunIdentity.system()

    assert first.run_id != second.run_id
    assert first.now().endswith("Z")


def test_builder_freezes_once_and_then_refuses_mutation() -> None:
    """One assembly, one freeze: two versions of the context reaching two
    consumers is a defect that is invisible at runtime."""
    builder = RunContextBuilder()
    identity = RunIdentity(run_id="r", now=lambda: "t")
    budget = _budget()

    context = builder.set_identity(identity).set_budget(budget).freeze()

    assert context.identity is identity
    assert context.budget is budget
    with pytest.raises(RuntimeError, match="frozen"):
        builder.set_identity(identity)
    with pytest.raises(RuntimeError, match="frozen"):
        builder.freeze()


def test_builder_setters_chain_and_cover_every_context_field() -> None:
    """A new RunContext field without a setter would be unreachable from the CLI."""
    setters = {
        name.removeprefix("set_")
        for name in dir(RunContextBuilder)
        if name.startswith("set_")
    }

    assert setters == {field.name for field in dataclasses.fields(RunContext)}


# --------------------------------------------------------------------------
# RunBudget: reserve / settle
# --------------------------------------------------------------------------


def test_reserve_then_settle_charges_the_real_usage_not_the_upper_bound() -> None:
    budget = _budget(max_usd=10.0)

    reservation = budget.reserve(
        input_tokens_upper=1_000_000,
        output_tokens_upper=100_000,
        timeout_s_upper=1.0,
    )
    reservation.settle(usage={"input_tokens": 10, "output_tokens": 5}, wall_s=0.5)

    snapshot = budget.snapshot()
    assert snapshot["attempts"] == 1
    assert snapshot["reserved_usd"] == pytest.approx(0.0)
    # 10/1e6 * 1.0 + 5/1e6 * 10.0
    assert snapshot["spent_usd"] == pytest.approx(6e-5)
    assert snapshot["wall_clock_s"] == pytest.approx(0.5)


def test_a_reservation_that_would_exceed_the_ceiling_is_refused_up_front() -> None:
    """The whole point of reserving: the call must not be made at all."""
    budget = _budget(max_usd=1.0)

    with pytest.raises(BudgetExhausted, match="cost"):
        budget.reserve(
            input_tokens_upper=2_000_000,
            output_tokens_upper=0,
            timeout_s_upper=1.0,
        )
    assert budget.snapshot()["attempts"] == 0
    assert budget.snapshot()["reserved_usd"] == pytest.approx(0.0)


def test_wall_clock_is_held_by_upper_bound_while_a_call_is_in_flight() -> None:
    """Never start a 120s request with 1s of allowance left: the ceiling would
    be blown before the call could possibly return. No time has elapsed here --
    the refusal comes from the *reserved* bound alone."""
    budget = _budget(max_usd=None, max_wall_clock_s=10.0)

    budget.reserve(input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=9.5)

    with pytest.raises(BudgetExhausted, match="wall clock"):
        budget.reserve(input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=1.0)


def test_settling_early_refunds_the_unused_wall_clock_hold() -> None:
    """A call that finished in 0.1s must not keep charging its 9.5s timeout,
    or one slow-timeout setting would cap the run at a single call."""
    budget = _budget(max_usd=None, max_wall_clock_s=10.0)

    budget.reserve(
        input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=9.5
    ).settle(usage={}, wall_s=0.1)

    assert budget.snapshot()["reserved_wall_clock_s"] == pytest.approx(0.0)
    assert budget.snapshot()["wall_clock_s"] == pytest.approx(0.1)
    # Only the 0.1s actually spent counts, so a second long call still fits.
    budget.reserve(input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=9.5)


def test_attempt_ceiling_counts_in_flight_reservations() -> None:
    budget = _budget(max_usd=None, max_attempts=1)

    outstanding = budget.reserve(
        input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=1.0
    )

    with pytest.raises(BudgetExhausted, match="attempts"):
        budget.reserve(input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=1.0)
    outstanding.settle(usage={}, wall_s=0.1)
    with pytest.raises(BudgetExhausted, match="attempts"):
        budget.reserve(input_tokens_upper=0, output_tokens_upper=0, timeout_s_upper=1.0)


# --------------------------------------------------------------------------
# RunBudget: the three terminal states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["settle", "settle_unknown", "release"])
def test_every_terminal_state_counts_exactly_one_attempt(terminal: str) -> None:
    """The reservation owns the counter; a caller-supplied running total would
    double count on the second attempt."""
    budget = _budget(max_usd=None)

    reservation = budget.reserve(
        input_tokens_upper=1000, output_tokens_upper=1000, timeout_s_upper=1.0
    )
    if terminal == "settle":
        reservation.settle(usage={"input_tokens": 1, "output_tokens": 1}, wall_s=0.1)
    else:
        getattr(reservation, terminal)(wall_s=0.1)

    assert budget.snapshot()["attempts"] == 1


def test_settle_unknown_keeps_the_reservation_charged_but_release_refunds_it() -> None:
    """An unknown outcome may already have been billed upstream; refunding it
    would understate the remaining budget and let the run overspend."""
    unknown = _budget(max_usd=None)
    released = _budget(max_usd=None)

    unknown.reserve(
        input_tokens_upper=1_000_000, output_tokens_upper=0, timeout_s_upper=1.0
    ).settle_unknown(wall_s=0.1)
    released.reserve(
        input_tokens_upper=1_000_000, output_tokens_upper=0, timeout_s_upper=1.0
    ).release(wall_s=0.1)

    assert unknown.snapshot()["spent_usd"] == pytest.approx(1.0)
    assert released.snapshot()["spent_usd"] == pytest.approx(0.0)


def test_an_unknown_outcome_eats_the_ceiling_for_the_next_call() -> None:
    budget = _budget(max_usd=1.0)

    budget.reserve(
        input_tokens_upper=1_000_000, output_tokens_upper=0, timeout_s_upper=1.0
    ).settle_unknown(wall_s=0.1)

    with pytest.raises(BudgetExhausted):
        budget.reserve(input_tokens_upper=1, output_tokens_upper=0, timeout_s_upper=1.0)


def test_a_reservation_cannot_be_terminated_twice() -> None:
    """Otherwise a retry path could refund the same reservation repeatedly."""
    budget = _budget(max_usd=None)
    reservation = budget.reserve(
        input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
    )
    reservation.settle(usage={}, wall_s=0.1)

    with pytest.raises(RuntimeError, match="already"):
        reservation.settle_unknown(wall_s=0.1)
    assert budget.snapshot()["attempts"] == 1


def test_overspend_is_recorded_not_rolled_back_and_stops_the_next_reserve() -> None:
    """Real usage above the reserved upper bound is money already spent."""
    budget = _budget(max_usd=1.0)

    budget.reserve(
        input_tokens_upper=1000, output_tokens_upper=0, timeout_s_upper=1.0
    ).settle(usage={"input_tokens": 5_000_000, "output_tokens": 0}, wall_s=0.1)

    assert budget.snapshot()["spent_usd"] == pytest.approx(5.0)
    with pytest.raises(BudgetExhausted):
        budget.reserve(input_tokens_upper=1, output_tokens_upper=0, timeout_s_upper=1.0)


def test_assert_all_settled_detects_a_leaked_reservation() -> None:
    """Called from the orchestrator's finally, never from __del__: destructor
    timing is not a reliable detector."""
    budget = _budget(max_usd=None)
    budget.assert_all_settled()

    reservation = budget.reserve(
        input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
    )

    with pytest.raises(RuntimeError, match="unsettled"):
        budget.assert_all_settled()
    reservation.release(wall_s=0.0)
    budget.assert_all_settled()


# --------------------------------------------------------------------------
# The property the whole object exists for
# --------------------------------------------------------------------------


def test_two_lanes_sharing_one_budget_cannot_double_spend_the_ceiling() -> None:
    """primary and fallback are distinct provider instances; if each held its
    own RunBudget a max_usd=1.0 run could spend 2.0."""
    shared = _budget(max_usd=1.0)
    primary_budget = shared
    fallback_budget = shared

    primary_budget.reserve(
        input_tokens_upper=900_000, output_tokens_upper=0, timeout_s_upper=1.0
    ).settle(usage={"input_tokens": 900_000, "output_tokens": 0}, wall_s=0.1)

    with pytest.raises(BudgetExhausted):
        fallback_budget.reserve(
            input_tokens_upper=900_000, output_tokens_upper=0, timeout_s_upper=1.0
        )


def test_prompt_token_upper_bound_rounds_up() -> None:
    """It must never *under*-state the input, or the reserve would be too small
    to be a ceiling. One shared estimator keeps callers from drifting apart."""
    assert prompt_tokens_upper("") == 0
    assert prompt_tokens_upper("a") == 1
    assert prompt_tokens_upper("abcd") == 1
    assert prompt_tokens_upper("abcde") == 2
    assert prompt_tokens_upper("ab", "cde") == 2


def test_snapshot_key_set_is_stable() -> None:
    """metrics.json consumers key off this; a silent rename breaks them."""
    assert set(_budget().snapshot()) == {
        "attempts",
        "max_attempts",
        "max_usd",
        "max_wall_clock_s",
        "reserved_usd",
        "reserved_wall_clock_s",
        "spent_usd",
        "wall_clock_s",
    }
