import pytest
from src.notifier import Notifier
from src.executor import Executor
from src.state import reconcile_positions
from src.risk_manager import RiskManager


def test_notifier_format_symbol():
    notifier = Notifier()
    # 기본: 미등록 종목인 경우 종목코드만 반환
    assert notifier.format_symbol("005930") == "005930"

    # 직접 name 전달 시
    assert notifier.format_symbol("005930", "삼성전자") == "005930(삼성전자)"

    # 등록 후
    notifier.register_symbol("005930", "삼성전자")
    assert notifier.format_symbol("005930") == "005930(삼성전자)"


def test_executor_label():
    config = {
        "execution": {},
        "cost": {},
        "risk": {"max_positions": 3, "risk_per_trade_pct": 1.0},
    }
    risk = RiskManager(config, 10000000)
    notifier = Notifier()
    executor = Executor(
        client=None,
        risk=risk,
        strategy=None,
        journal=None,
        notifier=notifier,
        config=config,
        dry_run=True,
    )

    # 미등록 시 종목코드 반환
    assert executor._label("000660") == "000660"

    # 종목명 등록 시 000660(SK하이닉스) 반환
    executor.register_symbol_name("000660", "SK하이닉스")
    assert executor._label("000660") == "000660(SK하이닉스)"
    assert notifier.format_symbol("000660") == "000660(SK하이닉스)"


def test_reconcile_positions_with_symbol_names():
    config = {
        "risk": {"max_positions": 3, "risk_per_trade_pct": 1.0},
    }
    risk = RiskManager(config, 10000000)
    symbol_names = {}

    holdings = [
        {
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "hldg_qty": "10",
            "pchs_avg_pric": "70000",
        }
    ]

    warnings = reconcile_positions(risk, holdings, symbol_names=symbol_names)

    assert symbol_names.get("005930") == "삼성전자"
    assert len(warnings) == 1
    assert "005930(삼성전자)" in warnings[0]


def test_kis_client_symbol_name_fallback():
    from src.kis_client import KISClient, KNOWN_SYMBOL_NAMES

    assert KNOWN_SYMBOL_NAMES.get("005930") == "삼성전자"
    assert KNOWN_SYMBOL_NAMES.get("001510") == "SK증권"

    # Mock auth & client
    client = KISClient(auth=None)
    assert client.get_symbol_name("005930") == "삼성전자"
    assert client.get_symbol_name("001510") == "SK증권"
    assert client.get_symbol_name("379810") == "KODEX 미국30년국채타겟유클리드"

