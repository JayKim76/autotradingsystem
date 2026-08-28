"""
kis_client.py — KIS Developers REST API 래퍼

v1.1 수정사항
  - 토큰버킷 레이트리미터 (KIS 유량 제한 대응)
  - 네트워크/5xx 오류 지수 백오프 재시도 (조회 계열만)
  - [중요] 주문은 자동 재시도하지 않고, 응답 유실 시 미체결 조회로 착지 여부 검증
    → 중복 주문(이중 매수) 방지
  - 체결 조회 API 추가 (실체결가 반영용)
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime
from typing import Any, Optional

import requests

from .auth import KISAuth

logger = logging.getLogger("autotrader.client")


# ──────────────────────────────────────────────
#  레이트 리미터 (토큰 버킷)
# ──────────────────────────────────────────────
class RateLimiter:
    """초당 요청 수 제한

    KIS API는 계정별 유량 제한이 있으며, 초과 시 일시 차단된다.
    실전 기준 초당 20건이지만 안전마진을 두고 기본 8건으로 설정한다.
    """

    def __init__(self, max_per_sec: int = 8) -> None:
        self.capacity = max_per_sec
        self._tokens  = float(max_per_sec)
        self._last    = time.monotonic()
        self._lock    = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.capacity)

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait = (1.0 - self._tokens) / self.capacity
                time.sleep(min(wait, 1.0))


class KISAPIError(RuntimeError):
    """KIS API 업무 오류 (rt_cd != 0)"""

    def __init__(self, tr_id: str, msg_cd: str, msg: str) -> None:
        self.tr_id  = tr_id
        self.msg_cd = msg_cd
        self.msg    = msg
        super().__init__(f"[{tr_id}] {msg_cd}: {msg}")


KNOWN_SYMBOL_NAMES: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "000270": "기아",
    "068270": "셀트리온",
    "035420": "NAVER",
    "035720": "카카오",
    "247540": "에코프로비엠",
    "086520": "에코프로",
    "196170": "알테오젠",
    "028300": "HLB",
    "022100": "포스코DX",
    "095340": "ISC",
    "058470": "리노공업",
    "277810": "레인보우로보틱스",
    "348370": "엔켐",
    "137400": "피엔티",
    "240810": "원익IPS",
    "042700": "한미반도체",
    "403870": "HPSP",
    "294090": "이오테크닉스",
    "001510": "SK증권",
    "379810": "KODEX 미국30년국채타겟유클리드",
    "379800": "KODEX 미국30년국채액티브",
    "229200": "KODEX 코스닥150선물인버스",
    "251340": "KODEX 코스닥150레버리지",
    "462860": "한국ANKOR유전",
}


class KISClient:
    """KIS REST API 래퍼"""

    # 재시도 대상 HTTP 상태
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, auth: KISAuth, max_per_sec: int = 8, max_retry: int = 4) -> None:
        self.auth      = auth
        self.limiter   = RateLimiter(max_per_sec)
        self.max_retry = max_retry
        self.symbol_names: dict[str, str] = KNOWN_SYMBOL_NAMES.copy()
        self._session  = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ── 저수준 요청 ───────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.auth.base_url}{path}"
        attempts = self.max_retry if retry else 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            self.limiter.acquire()
            try:
                headers = self.auth.get_headers(tr_id=tr_id)
                resp = self._session.request(
                    method, url, headers=headers,
                    params=params, json=body, timeout=15,
                )

                if resp.status_code in self._RETRY_STATUS and attempt < attempts:
                    backoff = min(2 ** attempt * 0.5, 8.0) + random.uniform(0, 0.3)
                    logger.warning(
                        "HTTP %d [%s] — %.1fs 후 재시도 (%d/%d)",
                        resp.status_code, tr_id, backoff, attempt, attempts,
                    )
                    time.sleep(backoff)
                    continue

                resp.raise_for_status()
                data = resp.json()

                if str(data.get("rt_cd", "0")) != "0":
                    raise KISAPIError(
                        tr_id,
                        str(data.get("msg_cd", "")),
                        str(data.get("msg1", data)),
                    )
                return data

            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < attempts:
                    backoff = min(2 ** attempt * 0.5, 8.0) + random.uniform(0, 0.3)
                    logger.warning(
                        "네트워크 오류 [%s] %s — %.1fs 후 재시도 (%d/%d)",
                        tr_id, e, backoff, attempt, attempts,
                    )
                    time.sleep(backoff)
                    continue
                raise
            except KISAPIError:
                raise                       # 업무 오류는 재시도 무의미

        if last_exc:
            raise last_exc
        raise RuntimeError(f"[{tr_id}] 요청 실패")

    def _get(self, path: str, params: dict, tr_id: str) -> dict[str, Any]:
        return self._request("GET", path, tr_id, params=params)

    def _post(self, path: str, body: dict, tr_id: str, retry: bool = True) -> dict[str, Any]:
        return self._request("POST", path, tr_id, body=body, retry=retry)

    # ── 시세 조회 ─────────────────────────────────────────────
    def get_price(self, symbol: str) -> dict[str, Any]:
        """현재가 조회"""
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            params, "FHKST01010100",
        )
        return data.get("output", {})

    def get_last_price(self, symbol: str) -> int:
        """현재가만 정수로 반환 (실패 시 0)"""
        try:
            return int(self.get_price(symbol).get("stck_prpr", 0))
        except Exception as e:
            logger.warning("[%s] 현재가 조회 실패: %s", symbol, e)
            return 0

    def get_orderbook(self, symbol: str) -> dict[str, Any]:
        """호가창 조회"""
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            params, "FHKST01010200",
        )
        return data.get("output1", {})

    def get_orderbook_totals(self, symbol: str) -> tuple[int, int]:
        """(총매도잔량, 총매수잔량) 반환"""
        try:
            ob = self.get_orderbook(symbol)
            return (
                int(ob.get("total_askp_rsqn", 0) or 0),
                int(ob.get("total_bidp_rsqn", 0) or 0),
            )
        except Exception as e:
            logger.warning("[%s] 호가 조회 실패: %s", symbol, e)
            return 0, 0

    def get_minute_candles(self, symbol: str, time_str: str = "") -> list[dict]:
        """당일 분봉 조회 (기준 시각 이전 최대 30건)"""
        if not time_str:
            time_str = datetime.now().strftime("%H%M%S")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": time_str,
            "FID_PW_DATA_INCU_YN": "Y",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            params, "FHKST03010200",
        )
        return data.get("output2", [])

    def get_minute_candles_full(self, symbol: str, anchors: Optional[list[str]] = None) -> list[dict]:
        """장 초반 전체 분봉 수집

        KIS 분봉 API는 1회 호출당 최대 30건만 반환하므로,
        여러 기준 시각으로 나눠 호출한 뒤 병합한다.
        (ORB 구간 09:00~09:30 데이터를 확보하기 위해 필수)
        """
        if anchors is None:
            anchors = ["093000", "103000", "113000", "133000", "143000", "153000"]

        now_hms = datetime.now().strftime("%H%M%S")
        merged: dict[str, dict] = {}

        for anchor in anchors:
            if anchor > now_hms:
                anchor = now_hms
            try:
                for row in self.get_minute_candles(symbol, anchor):
                    key = str(row.get("stck_cntg_hour", ""))
                    if key:
                        merged[key] = row
            except Exception as e:
                logger.debug("[%s] 분봉 수집 실패 (anchor=%s): %s", symbol, anchor, e)
            if anchor >= now_hms:
                break

        return list(merged.values())

    def get_historical_minute_candles(self, symbol: str, date_str: str, time_str: str = "153000") -> list[dict]:
        """과거 특정 영업일의 분봉 조회 (1회 호출 시 기준 시각 이전 최대 120건)"""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": date_str,
            "FID_INPUT_HOUR_1": time_str,
            "FID_ETC_CLS_CODE": "",
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_FAKE_TICK_INCU_YN": "N",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
            params, "FHKST03010230",
        )
        return data.get("output2", [])

    def get_historical_minute_candles_full(self, symbol: str, date_str: str) -> list[dict]:
        """과거 특정 영업일의 전체 분봉 수집 (09:00~15:30)"""
        anchors = ["104000", "124000", "144000", "153000"]
        merged: dict[str, dict] = {}
        for anchor in anchors:
            try:
                for row in self.get_historical_minute_candles(symbol, date_str, anchor):
                    key = str(row.get("stck_cntg_hour", ""))
                    if key:
                        merged[key] = row
            except Exception as e:
                logger.debug("[%s] 과거 분봉 수집 실패 (date=%s, anchor=%s): %s", symbol, date_str, anchor, e)
        return list(merged.values())


    def get_daily_candles(self, symbol: str, period: str = "D",
                          start: str = "", end: str = "") -> list[dict]:
        """일봉/주봉/월봉 조회"""
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start or "20240101",
            "FID_INPUT_DATE_2": end or today,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params, "FHKST03010100",
        )
        return data.get("output2", [])

    def get_index_status(self, iscd: str = "0001") -> dict[str, Any]:
        """업종/지수 현재가 및 등락률 조회 (0001: 코스피, 1001: 코스닥)"""
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": iscd,
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                params, "FHPUP02100000",
            )
            out = data.get("output", {})
            return {
                "price": float(out.get("bstp_nmix_prpr", 0) or 0),
                "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0),
            }
        except Exception as e:
            logger.warning("[지수 조회 실패] %s: %s", iscd, e)
            return {"price": 0.0, "change_pct": 0.0}

    def get_symbol_name(self, symbol: str) -> str:
        """종목코드 -> 종목명 조회 (메모리 캐시 및 API 폴백)"""
        if not symbol:
            return ""
        if symbol in self.symbol_names and self.symbol_names[symbol] != symbol:
            return self.symbol_names[symbol]

        # 1. get_price 시도
        try:
            d = self.get_price(symbol)
            name = str(d.get("hts_kor_isnm", "") or d.get("prdt_name", "") or d.get("rprs_mrkt_kor_name", "")).strip()
            if name and name != symbol:
                self.symbol_names[symbol] = name
                return name
        except Exception:
            pass

        # 2. search-stock-info (CTPF1604R) 시도
        try:
            params = {"PRDT_TYPE_CD": "300", "PDNO": symbol}
            data = self._get("/uapi/domestic-stock/v1/quotations/search-stock-info", params, "CTPF1604R")
            out = data.get("output", {})
            name = str(out.get("prdt_name", "") or out.get("hts_kor_isnm", "") or out.get("prdt_abrv_name", "")).strip()
            if name and name != symbol:
                self.symbol_names[symbol] = name
                return name
        except Exception:
            pass

        return ""

    def get_volume_rank(self) -> list[dict]:
        """거래량 순위 조회"""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         "0000",
            "FID_DIV_CLS_CODE":       "0",
            "FID_BLNG_CLS_CODE":      "0",
            "FID_TRGT_CLS_CODE":      "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "", "FID_INPUT_DATE_1": "",
        }
        try:
            data = self._get("/uapi/domestic-stock/v1/quotations/volume-rank", params, "FHPST01710000")
            rows = data.get("output", [])
            for r in rows:
                sym = str(r.get("mksc_shrn_iscd", "")).strip()
                name = str(r.get("hts_kor_isnm", "")).strip()
                if sym and name and name != sym:
                    self.symbol_names[sym] = name
            return rows
        except Exception as e:
            logger.warning("거래량 랭킹 조회 실패: %s", e)
            return []

    # ── 주문 ──────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,                 # "BUY" / "SELL"
        qty: int,
        price: int = 0,
        order_type: str = "00",    # 00=지정가, 01=시장가
    ) -> dict[str, Any]:
        """주식 매수/매도 주문

        주의: 이 메서드는 재시도하지 않는다.
              주문 API는 멱등하지 않아 재시도 시 이중 체결 위험이 있다.
              호출자는 실패 시 verify_order_landed() 로 착지 여부를 확인해야 한다.
        """
        is_paper = self.auth.mode != "real"

        if side == "BUY":
            tr_id, dvsn = ("VTTC0802U" if is_paper else "TTTC0802U"), "02"
        else:
            tr_id, dvsn = ("VTTC0801U" if is_paper else "TTTC0801U"), "01"

        body = {
            "CANO":            self.auth.cano,
            "ACNT_PRDT_CD":    self.auth.acnt_prdt,
            "PDNO":            symbol,
            "ORD_DVSN":        order_type,
            "ORD_QTY":         str(qty),
            "ORD_UNPR":        str(price) if order_type == "00" else "0",
            "SLL_BUY_DVSN_CD": dvsn,
        }

        logger.info(
            "주문 전송: %s %s %d주 @%s [%s]",
            side, symbol, qty, price if order_type == "00" else "시장가", tr_id,
        )
        return self._post(
            "/uapi/domestic-stock/v1/trading/order-cash", body, tr_id, retry=False
        )

    def verify_order_landed(self, symbol: str, side: str, qty: int) -> Optional[str]:
        """주문 응답 유실 시, 주문이 실제로 접수되었는지 확인

        Returns:
            접수된 주문번호 (없으면 None)
        """
        want = "02" if side == "BUY" else "01"
        try:
            for od in self.get_today_orders():
                if (od.get("pdno") == symbol
                        and od.get("sll_buy_dvsn_cd") == want
                        and int(od.get("ord_qty", 0) or 0) == qty):
                    odno = od.get("odno", "")
                    logger.warning("주문이 이미 접수되어 있음 — 재전송 취소 (odno=%s)", odno)
                    return odno
        except Exception as e:
            logger.error("주문 착지 검증 실패: %s", e)
        return None

    def cancel_order(self, odno: str, qty: int, symbol: str = "") -> dict[str, Any]:
        """주문 취소"""
        is_paper = self.auth.mode != "real"
        tr_id = "VTTC0803U" if is_paper else "TTTC0803U"
        body = {
            "CANO":               self.auth.cano,
            "ACNT_PRDT_CD":       self.auth.acnt_prdt,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO":          odno,
            "ORD_DVSN":           "00",
            "RVSE_CNCL_DVSN_CD":  "02",
            "ORD_QTY":            str(qty),
            "ORD_UNPR":           "0",
            "QTY_ALL_ORD_YN":     "Y",
        }
        return self._post(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl", body, tr_id, retry=False
        )

    # ── 체결 조회 ─────────────────────────────────────────────
    def get_today_orders(self) -> list[dict]:
        """당일 주문 체결 내역 조회"""
        is_paper = self.auth.mode != "real"
        tr_id = "VTTC8001R" if is_paper else "TTTC8001R"
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO":            self.auth.cano,
            "ACNT_PRDT_CD":    self.auth.acnt_prdt,
            "INQR_STRT_DT":    today,
            "INQR_END_DT":     today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN":       "00",
            "PDNO":            "",
            "CCLD_DVSN":       "00",
            "ORD_GNO_BRNO":    "",
            "ODNO":            "",
            "INQR_DVSN_3":     "00",
            "INQR_DVSN_1":     "",
            "CTX_AREA_FK100":  "",
            "CTX_AREA_NK100":  "",
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", params, tr_id
            )
            return data.get("output1", [])
        except Exception as e:
            logger.warning("주문 내역 조회 실패: %s", e)
            return []

    def get_fill_price(self, odno: str, fallback: float = 0.0) -> tuple[float, int]:
        """주문번호로 실제 평균 체결가·체결수량 조회

        Returns:
            (평균체결가, 체결수량) — 조회 실패 시 (fallback, 0)
        """
        try:
            for od in self.get_today_orders():
                if od.get("odno") == odno:
                    filled = int(od.get("tot_ccld_qty", 0) or 0)
                    avg    = float(od.get("avg_prvs", 0) or 0)
                    if filled > 0 and avg > 0:
                        return avg, filled
                    return fallback, filled
        except Exception as e:
            logger.warning("체결가 조회 실패 (odno=%s): %s", odno, e)
        return fallback, 0

    def get_open_orders(self) -> list[dict]:
        """미체결 주문 조회"""
        is_paper = self.auth.mode != "real"
        tr_id = "VTTC8036R" if is_paper else "TTTC8036R"
        params = {
            "CANO":            self.auth.cano,
            "ACNT_PRDT_CD":    self.auth.acnt_prdt,
            "CTX_AREA_FK100":  "",
            "CTX_AREA_NK100":  "",
            "INQR_DVSN_1":     "0",
            "INQR_DVSN_2":     "0",
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", params, tr_id
            )
            return data.get("output", [])
        except Exception as e:
            logger.warning("미체결 조회 실패: %s", e)
            return []

    # ── 잔고 조회 ─────────────────────────────────────────────
    def get_balance(self) -> dict[str, Any]:
        """주식 잔고 조회 (보유 종목 + 예수금)"""
        is_paper = self.auth.mode != "real"
        tr_id = "VTTC8434R" if is_paper else "TTTC8434R"
        params = {
            "CANO":            self.auth.cano,
            "ACNT_PRDT_CD":    self.auth.acnt_prdt,
            "AFHR_FLPR_YN":    "N",
            "OFL_YN":          "",
            "INQR_DVSN":       "02",
            "UNPR_DVSN":       "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN":       "01",
            "CTX_AREA_FK100":  "",
            "CTX_AREA_NK100":  "",
        }
        data = self._get("/uapi/domestic-stock/v1/trading/inquire-balance", params, tr_id)
        out2 = data.get("output2", [])
        return {
            "holdings": data.get("output1", []),
            "summary":  out2[0] if out2 else {},
        }

    def get_holdings(self) -> list[dict]:
        """보유 종목만 반환 (수량 > 0) 및 종목명 캐시 등록"""
        try:
            holdings = [
                h for h in self.get_balance().get("holdings", [])
                if int(h.get("hldg_qty", 0) or 0) > 0
            ]
            for h in holdings:
                sym = str(h.get("pdno", "")).strip()
                name = str(h.get("prdt_name", "") or h.get("hts_kor_isnm", "")).strip()
                if sym and name and name != sym:
                    self.symbol_names[sym] = name
            return holdings
        except Exception as e:
            logger.error("보유 종목 조회 실패: %s", e)
            return []

    def get_cash(self) -> float:
        """주문 가능 현금 반환"""
        try:
            summary = self.get_balance().get("summary", {})
            # ord_psbl_cash(주문가능현금)가 없으면 dnca_tot_amt(예수금총액) 사용
            for key in ("ord_psbl_cash", "dnca_tot_amt", "prvs_rcdl_excc_amt"):
                v = summary.get(key)
                if v not in (None, ""):
                    return float(v)
            return 0.0
        except Exception as e:
            logger.error("현금 잔고 조회 실패: %s", e)
            return 0.0

    def get_total_equity(self) -> float:
        """총 평가금액 (예수금 + 주식평가액)"""
        try:
            summary = self.get_balance().get("summary", {})
            v = summary.get("tot_evlu_amt")
            return float(v) if v not in (None, "") else self.get_cash()
        except Exception as e:
            logger.error("총 평가금액 조회 실패: %s", e)
            return 0.0
