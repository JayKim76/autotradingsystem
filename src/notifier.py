"""
notifier.py — 텔레그램 알림 (선택 기능)
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수가 없으면 로그만 출력
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("autotrader.notifier")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """텔레그램 알림 발송기"""

    def __init__(self) -> None:
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)

        self.symbol_names: dict[str, str] = {}

        if self.enabled:
            logger.info("텔레그램 알림 활성화 (chat_id=%s)", self.chat_id)
        else:
            logger.info("텔레그램 알림 비활성 (환경변수 미설정)")

    def register_symbol(self, symbol: str, name: str) -> None:
        """종목코드-종목명 매핑 등록"""
        if symbol and name and name != symbol:
            self.symbol_names[symbol] = name

    def format_symbol(self, symbol: str, name: str = "") -> str:
        """종목코드(종목명) 레이블 생성 (예: 005930(삼성전자))"""
        target_name = name or self.symbol_names.get(symbol, "")
        if target_name and target_name != symbol:
            return f"{symbol}({target_name})"
        return symbol

    def send(self, message: str) -> bool:
        """텔레그램 메시지 발송

        Returns:
            발송 성공 여부
        """
        logger.info("[알림] %s", message.replace("\n", " | "))

        if not self.enabled:
            return False

        try:
            url = _API.format(token=self.token)
            payload = {
                "chat_id":    self.chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("텔레그램 발송 실패: %s", e)
            return False

    def send_daily_report(self, summary: dict) -> None:
        """일일 마감 리포트 발송"""
        win_rate  = summary.get("win_rate", 0)
        net_pnl   = summary.get("net_pnl", 0)
        trades    = summary.get("trades", 0)
        wins      = summary.get("wins", 0)
        losses    = summary.get("losses", 0)

        emoji = "📈" if net_pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>일일 결산</b>\n"
            f"거래: {trades}회 ({wins}승 {losses}패 | 승률 {win_rate:.1f}%)\n"
            f"순손익: <b>{net_pnl:+,.0f}원</b>\n"
            f"수수료+세금: {summary.get('total_fee', 0) + summary.get('total_tax', 0):,.0f}원"
        )
        self.send(msg)
