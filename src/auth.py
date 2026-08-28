"""
auth.py — KIS Developers OAuth 토큰 / approval_key 관리
"""
from __future__ import annotations

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
from pathlib import Path

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("autotrader.auth")

# KIS API 기본 URL
_PAPER_BASE = "https://openapivts.koreainvestment.com:29443"
_REAL_BASE  = "https://openapi.koreainvestment.com:9443"


class KISAuth:
    """KIS Developers OAuth 토큰 관리 클래스"""

    def __init__(self) -> None:
        self.mode: str = os.getenv("TRADE_MODE", "paper").lower()

        if self.mode == "real":
            self.app_key    = os.environ["KIS_REAL_APP_KEY"]
            self.app_secret = os.environ["KIS_REAL_APP_SECRET"]
            self.cano       = os.environ["KIS_REAL_CANO"]
            self.acnt_prdt  = os.environ.get("KIS_REAL_ACNT_PRDT_CD", "01")
            self.base_url   = _REAL_BASE
        else:
            self.app_key    = os.environ["KIS_PAPER_APP_KEY"]
            self.app_secret = os.environ["KIS_PAPER_APP_SECRET"]
            self.cano       = os.environ["KIS_PAPER_CANO"]
            self.acnt_prdt  = os.environ.get("KIS_PAPER_ACNT_PRDT_CD", "01")
            self.base_url   = _PAPER_BASE

        self._access_token: str | None = None
        self._token_expires: datetime  = datetime.min.replace(tzinfo=KST)
        self._approval_key: str | None = None
        self._cache_file = Path(f"data/token_{self.mode}.json")
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_cached_token()

    def _load_cached_token(self) -> None:
        """디스크에서 기존 유효 토큰 로드 (재시작 시 tokenP 호출 절약)"""
        if not self._cache_file.exists():
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            exp = datetime.fromisoformat(d["expires_at"])
            if exp > datetime.now(tz=KST) + timedelta(minutes=5):
                self._access_token = d["token"]
                self._token_expires = exp
                logger.info("캐시된 KIS access_token 사용 (만료: %s)", exp.strftime("%H:%M:%S"))
        except Exception as e:
            logger.debug("토큰 캐시 로드 실패: %s", e)

    def _save_cached_token(self, token: str, expires_at: datetime) -> None:
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump({"token": token, "expires_at": expires_at.isoformat()}, f)
        except Exception as e:
            logger.warning("토큰 캐시 저장 실패: %s", e)

    # ── Access Token ─────────────────────────────────────────
    def _issue_token(self) -> None:
        """신규 access_token 발급 (재시도 로직 포함)"""
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "appsecret":  self.app_secret,
        }
        last_exc = None
        for attempt in range(1, 4):
            try:
                resp = requests.post(url, json=body, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                self._access_token = data["access_token"]
                expires_in = int(data.get("expires_in", 86400))
                self._token_expires = datetime.now(tz=KST) + timedelta(seconds=expires_in - 60)
                self._save_cached_token(self._access_token, self._token_expires)
                logger.info("KIS access_token 발급 완료 (만료: %s)", self._token_expires.strftime("%H:%M:%S"))
                return
            except Exception as e:
                last_exc = e
                logger.warning("토큰 발급 실패 [%d/3]: %s", attempt, e)
                time.sleep(2.0)

        # 발급 실패 시 연속 재호출 방지를 위해 1분간 임시 쿨다운 지정
        self._token_expires = datetime.now(tz=KST) + timedelta(seconds=60)
        if last_exc:
            raise last_exc

    def get_access_token(self) -> str:
        """유효한 access_token 반환 (만료 시 자동 갱신)"""
        if not self._access_token or datetime.now(tz=KST) >= self._token_expires:
            self._issue_token()
        return self._access_token  # type: ignore[return-value]

    # ── approval_key (WebSocket 용) ───────────────────────────
    def get_approval_key(self) -> str:
        """WebSocket 연결용 approval_key 반환"""
        if self._approval_key:
            return self._approval_key

        url = f"{self.base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "secretkey":  self.app_secret,
        }
        last_exc = None
        for attempt in range(1, 4):
            try:
                resp = requests.post(url, json=body, timeout=15)
                resp.raise_for_status()
                self._approval_key = resp.json()["approval_key"]
                logger.info("KIS approval_key 발급 완료")
                return self._approval_key  # type: ignore[return-value]
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                logger.warning("Approval 키 발급 네트워크 지연 [%d/3] — 재시도 중...", attempt)
                time.sleep(1.5)
        if last_exc:
            raise last_exc

    # ── 공통 헤더 빌더 ────────────────────────────────────────
    def get_headers(self, tr_id: str = "", content_type: str = "application/json") -> dict[str, str]:
        """REST 요청 공통 헤더 생성"""
        headers: dict[str, str] = {
            "Content-Type":  content_type,
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
        }
        if tr_id:
            headers["tr_id"] = tr_id
        return headers

    @property
    def account_no(self) -> str:
        """계좌번호 전체 (CANO + ACNT_PRDT_CD)"""
        return f"{self.cano}-{self.acnt_prdt}"
