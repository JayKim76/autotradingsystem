"""news_gate: 실시간 뉴스 호재/악재 게이트 회귀 테스트"""
from src.news_gate import NewsGate, classify_headlines


class FakeFetcher:
    def __init__(self, headlines=None, error=None):
        self.headlines = headlines or []
        self.error = error

    def get_news(self, symbol, limit=5):
        if self.error:
            raise self.error
        return self.headlines[:limit]


def test_classify_positive():
    v = classify_headlines([
        "A사, 1조원 규모 공급계약 체결… 수주잔고 사상 최대",
        "A사, 자사주 취득 결정… 배당 확대도 검토",
    ])
    assert v.label == "positive"
    assert v.score >= 2
    assert "공급계약" in v.positive_hits


def test_classify_negative_and_hard_negative():
    v = classify_headlines(["B사, 대규모 유상증자 결정… 주주 가치 희석 우려"])
    assert v.label == "negative"

    # 단독 키워드만으로 즉시 악재 확정
    hard = classify_headlines(["C사 대표, 횡령 혐의로 검찰 수사"])
    assert hard.label == "negative"
    assert "횡령" in hard.negative_hits


def test_classify_neutral_and_none():
    assert classify_headlines(["D사, 정기 주주총회 개최 안내"]).label == "neutral"
    assert classify_headlines([]).label == "none"


def test_gate_blocks_negative_allows_clean():
    gate = NewsGate(FakeFetcher(["E사, 임상 실패로 개발 중단"]), {})
    verdict = gate.evaluate("000001")
    allow, reason = gate.allows_entry(verdict)
    assert allow is False and "악재" in reason

    clean = NewsGate(FakeFetcher(["E사, 정기 주총 안내"]), {})
    allow2, _ = clean.allows_entry(clean.evaluate("000001"))
    assert allow2 is True


def test_gate_require_positive_mode():
    cfg = {"news_gate": {"require_positive": True}}
    neutral = NewsGate(FakeFetcher(["F사, 공시 안내"]), cfg)
    allow, _ = neutral.allows_entry(neutral.evaluate("000002"))
    assert allow is False                      # 호재 없으면 차단

    positive = NewsGate(FakeFetcher([
        "F사, 대형 수주 계약 체결", "F사, 흑자전환 성공… 최대 실적",
    ]), cfg)
    allow2, _ = positive.allows_entry(positive.evaluate("000002"))
    assert allow2 is True


def test_gate_fetch_failure_is_none_not_crash():
    gate = NewsGate(FakeFetcher(error=RuntimeError("network")), {})
    verdict = gate.evaluate("000003")
    assert verdict.label == "none"
    allow, _ = gate.allows_entry(verdict)
    assert allow is True                       # block 모드: 실패=미확인 허용

    strict = NewsGate(FakeFetcher(error=RuntimeError("network")),
                      {"news_gate": {"require_positive": True}})
    allow2, _ = strict.allows_entry(strict.evaluate("000003"))
    assert allow2 is False                     # require 모드: 미확인 차단


def test_gate_disabled_allows_everything():
    gate = NewsGate(FakeFetcher(["G사, 상장폐지 심사 대상"]),
                    {"news_gate": {"enabled": False}})
    allow, _ = gate.allows_entry(gate.evaluate("000004"))
    assert allow is True
