"""Test execution pipeline - failure handler, completion checker, pause controller."""
import pytest
from flowcraft_core.execution.failure_handler import (
    FailureType, FailureInfo, classify_exception, RETRY_POLICY, StepFailedError,
)
from flowcraft_core.execution.completion_checker import CompletionChecker
from flowcraft_core.execution.engine import PauseController, TaskCancelledError
from flowcraft_core.domain.enums import StepStatus
from flowcraft_core.domain.schemas import PlanStep


class TestFailureHandler:
    def test_classify_timeout(self):
        info = classify_exception(TimeoutError("Connection timed out"))
        assert info.failure_type == FailureType.TIMEOUT

    def test_classify_permission(self):
        info = classify_exception(PermissionError("Access denied"))
        assert info.failure_type == FailureType.PERMISSION_DENIED

    def test_classify_json_error(self):
        info = classify_exception(ValueError("JSON decode error at line 1"))
        assert info.failure_type == FailureType.MODEL_PARSE_ERROR

    def test_classify_model_error(self):
        info = classify_exception(RuntimeError("API rate limit exceeded"))
        assert info.failure_type == FailureType.MODEL_ERROR

    def test_terminal_errors_no_retry(self):
        for ft in (FailureType.PERMISSION_DENIED, FailureType.USER_REJECTED,
                    FailureType.POLICY_BLOCKED, FailureType.STEP_LIMIT):
            assert RETRY_POLICY[ft]["max_retries"] == 0
            assert RETRY_POLICY[ft]["terminal"] is True

    def test_transient_errors_can_retry(self):
        for ft in (FailureType.MODEL_ERROR, FailureType.TOOL_ERROR, FailureType.TIMEOUT):
            assert RETRY_POLICY[ft]["max_retries"] > 0
            assert RETRY_POLICY[ft]["terminal"] is False

    def test_failure_info_user_message(self):
        info = FailureInfo(FailureType.TIMEOUT, "test timeout")
        assert "超时" in info.user_message

    def test_step_failed_error(self):
        info = FailureInfo(FailureType.MODEL_ERROR, "model down")
        exc = StepFailedError(info)
        assert exc.failure_info.failure_type == FailureType.MODEL_ERROR


class TestCompletionChecker:
    def setup_method(self):
        self.checker = CompletionChecker()

    def test_empty_output(self):
        step = PlanStep(index=1, title="Test", objective="Test",
                        action_type="TOOL", expected_output="result", risk_level="LOW")
        result = self.checker.check_step(step, "")
        assert result.quality_score <= 0.5

    def test_error_keyword_detection(self):
        step = PlanStep(index=1, title="Test", objective="Test",
                        action_type="TOOL", expected_output="result", risk_level="LOW")
        result = self.checker.check_step(step, "Sorry, I cannot do that right now")
        assert result.quality_score < 1.0

    def test_good_output(self):
        step = PlanStep(index=1, title="Test", objective="Test",
                        action_type="TOOL", expected_output="result", risk_level="LOW")
        result = self.checker.check_step(step,
            "The file contains the following content: This is a test file with important data.")
        assert result.is_complete

    def test_duplicate_detection(self):
        step = PlanStep(index=1, title="Test", objective="Test",
                        action_type="TOOL", expected_output="result", risk_level="LOW")
        dup = "This is repeated content.\n" * 10
        result = self.checker.check_step(step, dup + dup)
        assert result.quality_score < 0.8

    def test_is_likely_complete(self):
        assert CompletionChecker.is_likely_complete("This is a complete answer with enough text")
        assert not CompletionChecker.is_likely_complete("")

    def test_needs_more_info(self):
        assert CompletionChecker.needs_more_info("请问你能提供更多信息吗？")
        assert not CompletionChecker.needs_more_info("The answer is 42.")


class TestPauseController:
    def setup_method(self):
        self.pc = PauseController()

    def test_initial_state(self):
        assert not self.pc.is_paused
        assert not self.pc.is_cancelled

    def test_pause_resume(self):
        self.pc.pause()
        assert self.pc.is_paused
        self.pc.resume()
        assert not self.pc.is_paused

    def test_cancel(self):
        self.pc.cancel()
        assert self.pc.is_cancelled
        with pytest.raises(TaskCancelledError):
            self.pc.check()

    def test_reset(self):
        self.pc.pause()
        self.pc.cancel()
        self.pc.reset()
        assert not self.pc.is_paused
        assert not self.pc.is_cancelled

