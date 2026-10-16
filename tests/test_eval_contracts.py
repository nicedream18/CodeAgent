import pytest
from pydantic import ValidationError

from mokioclaw.evaluation.models import Grade, legacy_result


def test_normal_terminal_does_not_mean_task_solved():
    assert not Grade(valid=True, tests_run=5, target_failed=1).solved


def test_empty_grade_cannot_claim_success():
    with pytest.raises(ValidationError):
        Grade(valid=True, solved=True)


def test_safety_demo_is_not_independent_task_success():
    assert legacy_result({'passed': True})['task_solved'] is None


def test_target_pass_with_regression_is_not_success():
    with pytest.raises(ValidationError):
        Grade(valid=True, solved=True, tests_run=5, target_passed=1, regression_failed=1)
