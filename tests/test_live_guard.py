"""Phase 0 실전 잠금 테스트 — real 차단 / paper 허용 / 명시 해제 시 허용"""
import pytest

from src.live_guard import assert_live_trading_allowed, live_trading_enabled

_PAPER_ENV = {
    "KIS_PAPER_APP_KEY": "test-paper-key",
    "KIS_PAPER_APP_SECRET": "test-paper-secret",
    "KIS_PAPER_CANO": "50012345",
}
_REAL_ENV = {
    "KIS_REAL_APP_KEY": "test-real-key",
    "KIS_REAL_APP_SECRET": "test-real-secret",
    "KIS_REAL_CANO": "12345678",
}


def _set_env(monkeypatch, values: dict):
    for k, v in values.items():
        monkeypatch.setenv(k, v)


def test_paper_and_dry_modes_always_allowed(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert_live_trading_allowed("paper")
    assert_live_trading_allowed("")
    assert not live_trading_enabled()


def test_real_blocked_without_unlock(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="Phase 0 잠금"):
        assert_live_trading_allowed("real")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "0")
    with pytest.raises(RuntimeError):
        assert_live_trading_allowed("real")


def test_real_allowed_with_explicit_unlock(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    assert live_trading_enabled()
    assert_live_trading_allowed("real")


def test_auth_paper_constructs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    _set_env(monkeypatch, _PAPER_ENV)
    monkeypatch.setenv("TRADE_MODE", "paper")
    from src.auth import KISAuth
    auth = KISAuth()
    assert auth.mode == "paper"


def test_auth_real_blocked_before_key_lookup(monkeypatch, tmp_path):
    # 잠금이 걸려 있으면 실전 키가 없어도 KeyError가 아니라 RuntimeError여야 한다
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("TRADE_MODE", "real")
    from src.auth import KISAuth
    with pytest.raises(RuntimeError, match="Phase 0 잠금"):
        KISAuth()


def test_auth_real_constructs_when_unlocked(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    _set_env(monkeypatch, _REAL_ENV)
    monkeypatch.setenv("TRADE_MODE", "real")
    from src.auth import KISAuth
    auth = KISAuth()
    assert auth.mode == "real"
