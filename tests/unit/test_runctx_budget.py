"""``RunBudget``'s two stricter ceilings and its per-cluster scope (DISTILL-001).

Both new limits ride the existing ``reserve()`` / ``Reservation`` path on
purpose: a second accounting path would let one call be charged twice, or once
against a ceiling nobody reads.
"""

from __future__ import annotations

import pytest

from memory_net.distiller.runctx import (
    BudgetExhausted,
    ClusterBudgetExhausted,
    RunBudget,
)


def _budget(**kwargs: float | None) -> RunBudget:
    return RunBudget(
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=1.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_soft_ceiling_above_the_hard_one_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="must not exceed max_usd"):
        _budget(max_usd=1.0, soft_max_usd=2.0)


def test_a_per_cluster_ceiling_above_the_hard_one_is_refused() -> None:
    with pytest.raises(ValueError, match="must not exceed max_usd"):
        _budget(max_usd=1.0, per_cluster_max_usd=1.5)


def test_a_stricter_ceiling_without_a_hard_one_is_refused() -> None:
    with pytest.raises(ValueError, match="needs a hard ceiling"):
        _budget(soft_max_usd=1.0)


def test_a_negative_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _budget(max_usd=1.0, per_cluster_max_usd=-0.1)


def test_the_soft_ceiling_does_not_refuse_a_call_already_scheduled() -> None:
    budget = _budget(max_usd=1.0, soft_max_usd=0.000_001)

    reservation = budget.reserve(
        input_tokens_upper=10,
        output_tokens_upper=10,
        timeout_s_upper=1.0,
    )
    reservation.settle(usage={"input_tokens": 10, "output_tokens": 10}, wall_s=0.1)

    # Crossed, and reported -- but nothing was refused, because winding down is
    # the scheduler's job and max_usd is the ceiling that actually stops a run.
    assert budget.soft_exhausted is True
    assert budget.snapshot()["soft_exhausted"] is True
    budget.reserve(
        input_tokens_upper=10, output_tokens_upper=10, timeout_s_upper=1.0
    ).release(wall_s=0.0)


def test_a_per_cluster_ceiling_only_bites_inside_its_scope() -> None:
    budget = _budget(max_usd=10.0, per_cluster_max_usd=0.000_002)

    with budget.enter_scope("cluster-0"):
        first = budget.reserve(
            input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
        )
        first.settle(usage={"input_tokens": 1, "output_tokens": 1}, wall_s=0.0)
        with pytest.raises(ClusterBudgetExhausted):
            budget.reserve(
                input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
            )

    # The next cluster starts from its own zero, and the run is untouched.
    with budget.enter_scope("cluster-1"):
        second = budget.reserve(
            input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
        )
        second.settle(usage={"input_tokens": 1, "output_tokens": 1}, wall_s=0.0)

    assert budget.scope_spent("cluster-0") == pytest.approx(2e-6)
    assert budget.scope_spent("cluster-1") == pytest.approx(2e-6)
    assert budget.spent_usd == pytest.approx(4e-6)


def test_outside_a_scope_the_per_cluster_ceiling_is_not_applied() -> None:
    budget = _budget(max_usd=10.0, per_cluster_max_usd=0.0)

    budget.reserve(
        input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
    ).release(wall_s=0.0)


def test_the_hard_ceiling_is_reported_when_both_would_refuse() -> None:
    budget = _budget(max_usd=0.000_001, per_cluster_max_usd=0.000_001)

    with budget.enter_scope("cluster-0"), pytest.raises(BudgetExhausted) as excinfo:
        budget.reserve(
            input_tokens_upper=10, output_tokens_upper=10, timeout_s_upper=1.0
        )

    assert not isinstance(excinfo.value, ClusterBudgetExhausted)
    assert "run budget exhausted" in str(excinfo.value)


def test_a_scope_counts_its_in_flight_reservations() -> None:
    budget = _budget(max_usd=10.0, per_cluster_max_usd=0.000_002)

    with budget.enter_scope("cluster-0"):
        held = budget.reserve(
            input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
        )
        with pytest.raises(ClusterBudgetExhausted):
            budget.reserve(
                input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
            )
        held.release(wall_s=0.0)
        # Released: the scope is free again, so the retry is allowed.
        budget.reserve(
            input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0
        ).release(wall_s=0.0)


def test_two_lanes_sharing_one_budget_do_not_double_spend() -> None:
    budget = _budget(max_usd=10.0)
    primary = budget
    fallback = budget

    with budget.enter_scope("cluster-0"):
        primary.reserve(
            input_tokens_upper=1_000, output_tokens_upper=1_000, timeout_s_upper=1.0
        ).settle(usage={"input_tokens": 1_000, "output_tokens": 1_000}, wall_s=0.0)
        fallback.reserve(
            input_tokens_upper=1_000, output_tokens_upper=1_000, timeout_s_upper=1.0
        ).settle(usage={"input_tokens": 1_000, "output_tokens": 1_000}, wall_s=0.0)

    assert budget.attempts == 2
    assert budget.spent_usd == pytest.approx(4e-3)
    assert budget.scope_spent("cluster-0") == pytest.approx(budget.spent_usd)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_ceiling_is_refused(value: float) -> None:
    """NaN compares false against every projection, so it caps nothing."""
    with pytest.raises(ValueError, match="finite"):
        _budget(max_usd=1.0, per_cluster_max_usd=value)
    with pytest.raises(ValueError, match="finite"):
        _budget(max_usd=1.0, soft_max_usd=value)
    with pytest.raises(ValueError, match="finite"):
        _budget(max_usd=value)
