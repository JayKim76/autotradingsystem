import pytest
import json
from src.ai_agent import LocalAIAgent


def test_ai_agent_daily_review_loss_analysis(tmp_path):
    config = {
        "ai": {
            "enabled": True,
            "data_dir": str(tmp_path),
        }
    }
    agent = LocalAIAgent(config)

    # Mock _call_ai_raw response
    mock_response = json.dumps({
        "review": "오늘 총 2건 거래 중 1건 손실 발생",
        "loss_analysis": ["005930(삼성전자): 지지선 붕괴 후 무리한 매수 진입"],
        "lessons": ["지지선 이탈 시 매수 금지"],
        "grade": "C"
    })
    agent._call_ai_raw = lambda sys_prompt, user_prompt, timeout=10.0: mock_response

    trades = [
        {
            "timestamp": "2026-08-21 10:00:00",
            "symbol": "005930",
            "side": "SELL",
            "qty": 10,
            "price": 68000,
            "entry_price": 70000,
            "exit_reason": "손절",
            "pnl": -20000,
            "pnl_pct": -2.85,
        }
    ]

    symbol_names = {"005930": "삼성전자"}
    report = agent.daily_review(trades, symbol_names=symbol_names)

    assert "🧠 <b>AI 매매 복기 및 학습</b> (등급: C)" in report
    assert "❌ <b>손실 원인 및 판단 미스 분석</b>" in report
    assert "005930(삼성전자)" in report
    assert len(agent.lessons) > 0
    assert any("손실원인: 005930(삼성전자)" in l for l in agent.lessons)
