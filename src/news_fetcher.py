import logging
import re
import time
import requests
import bs4
import yfinance as yf
from typing import List, Dict, Any, Optional

logger = logging.getLogger("autotrader.news_fetcher")


class NewsFetcher:
    """국내/해외 주식 실시간 뉴스 수집 모듈
    
    - 국내주식 (KOSPI/KOSDAQ 6자리 코드): 네이버 금융 실시간 뉴스 수집
    - 해외주식 (미국 주식 티커): yfinance news 수집
    - 10분 캐싱으로 중복 Scraping / API 과도 호출 방지
    """

    def __init__(self, cache_ttl_sec: int = 600):
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get_news(self, symbol: str, limit: int = 5) -> List[str]:
        """종목별 최신 뉴스 헤드라인 수집 (국내/해외 자동 판별)"""
        now = time.time()
        if symbol in self._cache:
            entry = self._cache[symbol]
            if now - entry["timestamp"] < self.cache_ttl_sec:
                return entry["news"][:limit]

        news_titles: List[str] = []
        clean_symbol = symbol.strip().upper()

        # 한국 주식 (6자리 숫자) vs 해외 주식 (영문 알파벳)
        if re.match(r"^\d{6}$", clean_symbol):
            news_titles = self._fetch_naver_news(clean_symbol, limit=limit)
        else:
            news_titles = self._fetch_yfinance_news(clean_symbol, limit=limit)

        self._cache[symbol] = {
            "timestamp": now,
            "news": news_titles,
        }
        return news_titles[:limit]

    def _fetch_naver_news(self, symbol: str, limit: int = 5) -> List[str]:
        """네이버 금융 개별 종목 최신 뉴스 수집"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        url = f"https://finance.naver.com/item/main.naver?code={symbol}"
        titles: List[str] = []

        try:
            resp = requests.get(url, headers=headers, timeout=5)
            # 네이버 금융은 UTF-8 meta charset을 사용 → requests 자동 감지
            if resp.status_code == 200:
                soup = bs4.BeautifulSoup(resp.text, "html.parser")
                # 1. news_section 헤드라인
                for a in soup.select("div.news_section ul li a"):
                    txt = a.text.strip()
                    if txt and txt not in titles and "뉴스 더보기" not in txt:
                        titles.append(txt)
                # 2. 공시 / 주요 뉴스 테이블
                for a in soup.select("table.type5 td.title a"):
                    txt = a.text.strip()
                    if txt and txt not in titles:
                        titles.append(txt)
        except Exception as e:
            logger.warning("[%s] 네이버 뉴스 수집 실패: %s", symbol, e)

        return titles[:limit]

    def _fetch_yfinance_news(self, symbol: str, limit: int = 5) -> List[str]:
        """yfinance 미국 주식 최신 뉴스 수집
        
        yfinance >= 1.6 에서 뉴스 구조:
          [{"id": ..., "content": {"title": ..., "summary": ...}}, ...]
        이전 버전 구조:
          [{"title": ..., "link": ...}, ...]
        """
        titles: List[str] = []
        try:
            ticker = yf.Ticker(symbol)
            raw_news = getattr(ticker, "news", []) or []
            for item in raw_news:
                if not isinstance(item, dict):
                    # object 형태
                    title = str(getattr(item, "title", "")).strip()
                    if title:
                        titles.append(title)
                    continue

                # 신버전: {"id": ..., "content": {"title": ...}}
                if "content" in item and isinstance(item["content"], dict):
                    title = item["content"].get("title", "").strip()
                else:
                    # 구버전: {"title": ..., "link": ...}
                    title = (item.get("title") or item.get("headline", "")).strip()

                if title:
                    titles.append(title)
        except Exception as e:
            logger.warning("[%s] yfinance 뉴스 수집 실패: %s", symbol, e)

        return titles[:limit]
