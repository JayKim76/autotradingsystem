# autotradingsystem

한국투자증권(KIS) API 기반 국내주식 1분봉 눌림목 자동매매 시스템.

## 전략 요약

- 거래량 상위 종목 스크리닝(시총·거래대금·스프레드 필터) → 09:00~09:30 ORB 관찰
- 상승 추세 + VWAP/MA 지지 눌림 + 반등 확인을 100점 스코어링으로 판정
- 로컬 LLM(LM Studio) 최종 승인 게이트 — 장애 시 진입 보류(fail-closed)가 기본
- 청산: ATR 손절(진입 시 고정), R배수 익절 사다리(TP1 +2.0R 33% · TP2 +3.5R 33%),
  +1.0R부터 트레일링, 15:08부터 분할 강제청산(오버나잇 없음)
- 리스크: 거래당 1.5%, 포지션 상한 30%, 동시보유 2종목, 일일 -2%·월간 -10% 중단
- 실시간 시세는 WebSocket 피드 우선, 오래되거나 끊기면 REST 폴링으로 자동 대체

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # KIS API 키 입력 (.env는 커밋 금지)
```

## 실행

```bash
python main.py --dry-run        # 주문 없이 신호만
python main.py                  # 실전/모의 (TRADE_MODE로 구분)
python main.py --config config.yaml --config-override config_optimized.yaml
python main.py --backtest data/candles   # 백테스트
python main.py --scenarios               # 시장 국면별 시나리오 검증
python scripts/run_walkforward.py --optimize   # 실데이터 워크포워드 검증 (백테스트검증가이드.md 참조)
```

설정 파일(`config_optimized.yaml` 등)은 전체 복제본이 아니라 기본 설정과의
차이(override)만 담는다. `src/config_loader.py`가 병합한다.

## 테스트

```bash
pip install pytest
python -m pytest tests/ -q
```

## 주의

- 휴장일 표는 `src/utils.py`의 `KRX_HOLIDAYS_BY_YEAR`에서 연도별로 관리한다.
  표에 없는 연도를 조회하면 경고 로그가 남는다 — 매년 말 다음 해 표를 추가할 것.
- 모의투자(paper)로 충분히 검증한 뒤 실전(real) 전환을 권장한다.
  이 소프트웨어는 투자 조언이 아니며, 매매 손익의 책임은 사용자에게 있다.

자세한 사용법은 `사용메뉴얼.md`, 백테스트는 `백테스트가이드.md` 참조.
