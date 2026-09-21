# Paper Trader v0.1

토스증권 실시간 시세를 선택적으로 사용해 가상자금으로 자동매매하는 FastAPI MVP입니다.

> v0.1은 안전을 위해 `paper` 모드로 고정되어 있으며 실제 주문 기능이 없습니다.

## 주요 기능

- KRW·USD 가상계좌와 SQLite 영속화
- 가격 임계값 전략 자동 실행
- 수수료, 슬리피지 및 주문금액 한도 적용
- 중복 주문 방지
- 엔진 시작·중지와 킬 스위치
- 선택적 토스증권 현재가 조회
- 자격 증명 없이 사용할 수 있는 수동 시세 입력 API
- FastAPI Swagger UI

## 설치 및 실행

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

브라우저에서 <http://127.0.0.1:8000/docs>를 열면 API를 사용할 수 있습니다.

환경 변수는 [.env.example](.env.example)을 참고하세요. 현재 구현은 `.env` 파일을 자동으로 읽지 않으므로 PowerShell 환경 변수 또는 실행 환경에서 직접 주입합니다.

```powershell
$env:TOSS_CLIENT_ID="발급받은 client id"
$env:TOSS_CLIENT_SECRET="발급받은 client secret"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

토스 자격 증명을 설정하지 않으면 `manual` 시세 모드로 실행됩니다. 자격 증명을 설정해도 v0.1은 현재가만 조회하며 계좌 및 주문 API를 호출하지 않습니다.

## 빠른 사용 순서

### 1. 전략 등록

```powershell
$body = @{
  strategy_id = "samsung-demo"
  symbol = "005930"
  currency = "KRW"
  quantity = "1"
  buy_below = "70000"
  sell_above = "75000"
  enabled = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/strategies" `
  -ContentType "application/json" -Body $body
```

### 2. 수동 시세 입력

토스 자격 증명이 없는 경우 테스트 가격을 입력합니다.

```powershell
$quote = @{
  symbol = "005930"
  price = "69000"
  currency = "KRW"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/market/quotes" `
  -ContentType "application/json" -Body $quote
```

### 3. 엔진 시작

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/engine/start"
```

현재가가 `buy_below` 이하이고 해당 종목을 보유하지 않았다면 가상 매수합니다. 보유 중 현재가가 `sell_above` 이상이면 가상 매도합니다.

### 4. 결과 확인

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/paper/account"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/orders"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/status"
```

### 5. 중지 또는 긴급 차단

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/engine/stop"

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/risk/kill-switch"
```

## 테스트

```powershell
python -m unittest discover -v
python -m compileall -q app tests
```

## 현재 제한사항

- 주문은 시장가 즉시 체결 모델입니다.
- 부분 체결, 호가 잔량, 세금 및 거래소별 호가 단위는 아직 정밀 모사하지 않습니다.
- 토스 WebSocket은 아직 연결하지 않고 현재가 REST API만 사용합니다.
- 관리 API 인증과 UI는 아직 없습니다. 외부 네트워크에 공개하지 마세요.
- 실제 계좌 주문은 구현하지 않았습니다.

상세 요구사항은 [prd.md](prd.md)를 참고하세요.
