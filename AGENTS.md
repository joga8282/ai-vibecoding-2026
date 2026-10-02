# 저장소 작업 지침

## 프로젝트 개요

- Python과 FastAPI로 구현한 주식 자동매매 대시보드입니다. 기본 동작은 PAPER이며, 현재 자동매매 전략은 대형주 테마 스윙입니다.
- `app/static/`에는 정적 프론트엔드가 있습니다. API는 `app/main.py`에서 구성하고, 핵심 거래 로직은 `app/` 모듈에 둡니다.
- `data/`와 `.env`는 사용자별 로컬 상태와 비밀정보를 담습니다. 내용을 출력하거나 커밋하지 말고, 초기화·삭제·덮어쓰기도 요청 없이 하지 마세요.
- 현재 코드와 README 첫 부분을 구현 동작의 기준으로 삼으세요. README 아래쪽의 이전 전략 설명과 `issue.md`의 미완료 작업은 현재 활성 전략을 뜻하지 않습니다.

## 안전 경계

- PAPER가 기본 모드입니다. LIVE 모드는 토스 계좌 읽기 전용 동기화이며 실제 주문 전송은 잠겨 있습니다.
- `TRADER_MODE`, DB 경로, 주문 동작을 바꾸거나 실주문 경로를 추가하는 일은 명시적 요청과 별도 검증 없이는 하지 마세요.
- 테스트에서는 실제 네트워크·계좌·주문에 접근하지 않도록 모의 객체를 사용하세요.
- 금융 전략의 변경은 신호 조건, 데이터 신선도, 포지션 관리와 기존 보유분 동작에 미치는 영향을 확인하고 관련 테스트를 보강하세요. 추천 결과나 전략 수익을 보장하는 표현은 사용하지 마세요.

## 주요 파일

- `app/config.py`: 환경 설정과 PAPER/LIVE 시작 검증
- `app/main.py`: FastAPI 앱, API 경로, 앱 수명 주기
- `app/engine.py`, `app/paper.py`, `app/repository.py`: 엔진, 가상 브로커, SQLite 저장소
- `app/swing_trader.py`, `app/swing_signals.py`, `app/swing_universe.py`: 현재 스윙 자동매매·신호·대상 종목
- `app/swing_recommendations.py`, `app/recommendation_service.py`: 추천 후보 생성
- `app/brokers/`: 브로커 인터페이스와 토스 읽기 전용 연동
- `app/static/`: 화면 HTML, CSS, JavaScript
- `tests/`: `unittest` 기반 단위 및 비동기 테스트

## 개발 및 검증

- 의존성 설치: `python -m pip install -r requirements.txt`
- 전체 테스트: `python -m unittest discover -v`
- 로컬 서버: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`
- 변경한 동작에 가장 가까운 테스트부터 실행하고, 공용 계약이나 거래 흐름을 바꾼 경우 전체 테스트도 실행하세요.
- 사용자 요청에 필요하지 않은 포맷 변경이나 무관한 리팩터링은 피하세요. 공개 API와 저장 데이터 형식은 기존 호환성을 유지하세요.
- 설정 예시는 `.env.example`에 두고 실제 키·토큰·계좌 정보를 넣지 마세요.
