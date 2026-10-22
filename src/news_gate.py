"""
news_gate.py — 종목 선정 직후 실시간 뉴스 게이트

역할:
  스크리너/전략 신호로 종목이 선정되면, 진입 전에 최신 뉴스 헤드라인을
  호재/악재로 분류해 매매에 반영한다. LLM 단독 판단(ai_agent)과 달리
  키워드 기반이라 결정적(deterministic)이고 사유를 그대로 로그에 남길 수 있다.

정책 (config.yaml news_gate 섹션):
  - block_on_negative (기본 true): 악재로 분류되면 진입 차단
  - require_positive (기본 false): true면 호재가 확인될 때만 진입 허용
    (뉴스가 없는 평상시까지 전부 막히므로 기본값은 false)
  - 수집 실패/뉴스 없음은 "none"으로 취급한다 — block 모드에서는 허용,
    require 모드에서는 미확인으로 차단된다.

주의: 백테스트(동결 규칙 포함)에는 적용하지 않는다. 과거 특정 시점의
실시간 뉴스는 복원할 수 없어 검증이 불가능하기 때문이다. 실전 경로 전용.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("autotrader.news_gate")

# 호재 키워드 (제목에 포함되면 +1, 헤드라인당 키워드별 1회)
POSITIVE_KEYWORDS = [
    "수주", "공급계약", "계약 체결", "대규모 수주", "수주잔고",
    "흑자전환", "최대 실적", "사상 최대", "어닝 서프라이즈", "깜짝 실적",
    "영업익 급증", "매출 급증", "실적 개선", "목표주가 상향", "목표가 상향",
    "승인", "허가 획득", "임상 성공", "임상 3상 성공", "특허 취득", "특허 등록",
    "자사주 취득", "자사주 소각", "배당 확대", "배당 상향", "무상증자",
    "지분 취득", "경영권 인수", "합병 승인", "정부 수주", "국책과제 선정",
    "양산 시작", "신공장", "증설", "수출 계약", "해외 진출", "독점 공급",
    "상향 조정", "이익 전망 상향", "신용등급 상향",
]

# 악재 키워드 (제목에 포함되면 -1)
NEGATIVE_KEYWORDS = [
    "유상증자", "전환사채", "신주인수권", "감자 결정",
    "적자전환", "적자 확대", "어닝 쇼크", "실적 악화", "매출 감소", "영업손실",
    "목표주가 하향", "목표가 하향", "하향 조정", "신용등급 하향",
    "소송 제기", "피소", "과징금", "제재", "경고 조치",
    "리콜", "판매 중단", "영업정지", "생산 중단", "공장 화재", "화재 발생",
    "파업", "노사 갈등", "임상 실패", "임상 중단", "허가 반려", "승인 반려",
    "횡령", "배임", "분식회계", "거래정지", "관리종목", "상장폐지",
    "대표이사 사임", "최대주주 변경", "지분 매각", "블록딜",
    "공매도", "보호예수 해제", "대규모 매도",
]

# 단독으로 즉시 악재 확정인 키워드 (점수와 무관하게 negative)
HARD_NEGATIVE_KEYWORDS = [
    "횡령", "배임", "분식회계", "거래정지", "관리종목 지정", "상장폐지",
    "영업정지", "임상 실패", "허가 반려",
]


@dataclass
class NewsVerdict:
    symbol: str
    label: str                      # positive | negative | neutral | none
    score: int = 0
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.positive_hits:
            parts.append(f"호재 {len(self.positive_hits)}건({', '.join(self.positive_hits[:3])})")
        if self.negative_hits:
            parts.append(f"악재 {len(self.negative_hits)}건({', '.join(self.negative_hits[:3])})")
        if not parts:
            parts.append("특이 뉴스 없음")
        return " · ".join(parts)


def classify_headlines(headlines: list[str], positive_threshold: int = 2) -> NewsVerdict:
    """헤드라인 목록 → 판정. symbol은 호출자가 채운다."""
    score = 0
    pos_hits: list[str] = []
    neg_hits: list[str] = []
    hard_negative = False
    for title in headlines or []:
        for kw in POSITIVE_KEYWORDS:
            if kw in title:
                score += 1
                pos_hits.append(kw)
        for kw in NEGATIVE_KEYWORDS:
            if kw in title:
                score -= 1
                neg_hits.append(kw)
        if any(kw in title for kw in HARD_NEGATIVE_KEYWORDS):
            hard_negative = True

    if hard_negative or score <= -1:
        label = "negative"
    elif score >= positive_threshold:
        label = "positive"
    elif headlines:
        label = "neutral"
    else:
        label = "none"
    return NewsVerdict(symbol="", label=label, score=score,
                       positive_hits=pos_hits, negative_hits=neg_hits,
                       headlines=list(headlines or []))


class NewsGate:
    """실시간 뉴스 게이트. fetcher는 get_news(symbol, limit)를 제공하면 된다."""

    def __init__(self, fetcher=None, config: Optional[dict] = None):
        section = (config or {}).get("news_gate", {}) or {}
        self.enabled = bool(section.get("enabled", True))
        self.block_on_negative = bool(section.get("block_on_negative", True))
        self.require_positive = bool(section.get("require_positive", False))
        self.positive_threshold = int(section.get("positive_threshold", 2))
        self.headlines_limit = int(section.get("headlines_limit", 8))
        self.fetcher = fetcher

    def evaluate(self, symbol: str) -> NewsVerdict:
        """최신 뉴스를 수집해 분류한다. 실패하면 none 판정 (예외를 올리지 않는다)."""
        headlines: list[str] = []
        if self.fetcher is not None:
            try:
                headlines = self.fetcher.get_news(symbol, limit=self.headlines_limit) or []
            except Exception as e:  # 수집 실패가 매매 루프를 죽이면 안 된다
                logger.warning("[%s] 뉴스 수집 실패 — none으로 처리: %s", symbol, e)
                headlines = []
        verdict = classify_headlines(headlines, self.positive_threshold)
        verdict.symbol = symbol
        return verdict

    def allows_entry(self, verdict: NewsVerdict) -> tuple[bool, str]:
        """(진입 허용 여부, 사유)"""
        if not self.enabled:
            return True, "뉴스 게이트 비활성"
        if self.block_on_negative and verdict.label == "negative":
            return False, f"악재 뉴스 차단 — {verdict.summary}"
        if self.require_positive and verdict.label != "positive":
            return False, f"호재 미확인 (판정 {verdict.label}) — {verdict.summary}"
        return True, f"뉴스 게이트 통과 ({verdict.label}) — {verdict.summary}"
