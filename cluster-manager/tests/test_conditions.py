"""The canary judgement: pure functions, no database."""

from cluster_manager.daemon.services.conditions import canary_rejected, count_outcomes
from cluster_manager.schemas import CanaryLimits, CanaryOutcome

LENIENT = CanaryLimits(max_crashes=5, max_violations=5, max_not_converged=5)
STRICT = CanaryLimits(max_crashes=0, max_violations=1, max_not_converged=0)


def counts(*outcomes: CanaryOutcome) -> dict[CanaryOutcome, int]:
    return count_outcomes(outcomes)


def test_all_passed_promotes() -> None:
    assert not canary_rejected(counts(CanaryOutcome.PASSED, CanaryOutcome.PASSED), STRICT)


def test_no_verdict_counts_as_converged() -> None:
    assert not canary_rejected(counts(CanaryOutcome.NO_VERDICT), STRICT)


def test_nothing_converged_rejects_even_within_the_limits() -> None:
    assert canary_rejected(counts(CanaryOutcome.NOT_CONVERGED, CanaryOutcome.NOT_CONVERGED), LENIENT)
    assert canary_rejected(counts(CanaryOutcome.CRASHED), LENIENT)


def test_an_empty_canary_rejects() -> None:
    assert canary_rejected(counts(), LENIENT)          # every canary agent decommissioned


def test_one_not_converged_rejects_at_zero() -> None:
    assert canary_rejected(counts(CanaryOutcome.PASSED, CanaryOutcome.NOT_CONVERGED), STRICT)


def test_buckets_over_their_limits_reject() -> None:
    assert canary_rejected(counts(CanaryOutcome.PASSED, CanaryOutcome.FAULTY_UPDATE), STRICT)
    assert canary_rejected(counts(CanaryOutcome.PASSED, CanaryOutcome.PERFORMANCE,
                                  CanaryOutcome.PERFORMANCE), STRICT)
    assert not canary_rejected(counts(CanaryOutcome.PASSED, CanaryOutcome.PERFORMANCE), STRICT)
