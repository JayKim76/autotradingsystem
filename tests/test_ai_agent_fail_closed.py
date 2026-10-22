"""AI 게이트 fail-closed 회귀 테스트

AI 타임아웃·오류·파싱 실패 시 기본 동작은 진입 보류(trade=False)여야 한다.
fail_open_on_error=True 를 명시한 경우에만 규칙 기반 통과로 대체한다.
"""
import json

import requests

from src.ai_agent import LocalAIAgent


def _agent(tmp_path, **ai_extra):
    config = {"ai": {"enabled": True, "data_dir": str(tmp_path), **ai_extra}}
    return LocalAIAgent(config)


def test_ai_timeout_fails_closed(tmp_path):
    agent = _agent(tmp_path)

    def _timeout(sys_prompt, user_prompt, timeout=3.0):
        raise requests.exceptions.Timeout("slow")

    agent._call_ai_raw = _timeout
    result = agent._call_ai("sys", "user", "005930")
    assert result["trade"] is False
    assert result["confidence"] == 0


def test_ai_error_fails_closed(tmp_path):
    agent = _agent(tmp_path)

    def _boom(sys_prompt, user_prompt, timeout=3.0):
        raise RuntimeError("server down")

    agent._call_ai_raw = _boom
    result = agent._call_ai("sys", "user", "005930")
    assert result["trade"] is False


def test_ai_invalid_json_fails_closed(tmp_path):
    agent = _agent(tmp_path)
    agent._call_ai_raw = lambda sys_prompt, user_prompt, timeout=3.0: "JSON 아님"
    result = agent._call_ai("sys", "user", "005930")
    assert result["trade"] is False


def test_ai_approve_and_confidence_threshold(tmp_path):
    agent = _agent(tmp_path)
    agent._call_ai_raw = lambda s, u, timeout=3.0: json.dumps(
        {"trade": True, "confidence": 80, "reason": "지지 반등"}
    )
    assert agent._call_ai("sys", "user", "005930")["trade"] is True

    agent._call_ai_raw = lambda s, u, timeout=3.0: json.dumps(
        {"trade": True, "confidence": 50, "reason": "애매함"}
    )
    # 확신도 65% 미만 승인은 거절 (기존 동작 유지)
    assert agent._call_ai("sys", "user", "005930")["trade"] is False


def test_ai_fail_open_is_opt_in_only(tmp_path):
    agent = _agent(tmp_path, fail_open_on_error=True)

    def _timeout(sys_prompt, user_prompt, timeout=3.0):
        raise requests.exceptions.Timeout("slow")

    agent._call_ai_raw = _timeout
    result = agent._call_ai("sys", "user", "005930")
    assert result["trade"] is True
