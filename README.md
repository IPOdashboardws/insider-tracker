# Insider Tape — 한·일·미 내부자 매수 일일 추적기

매일 한국·일본·미국 시장 마감 후 내부자(또는 대주주) 매수 공시를 자동으로 수집·정리하는 개인용 대시보드.

- **한국:** DART OpenAPI에서 임원·주요주주특정증권등소유상황보고서 수집, 장내매수 + 임원/이사 + 1억원 이상만
- **일본:** EDINET API에서 大量保有報告書(대량보유보고서) 수집, 5%+ 보유자의 매수성 변동(040 신규 / 050 추가매수)
- **미국:** SEC EDGAR 일일 인덱스에서 Form 4 수집, 거래코드 P + 임원/이사 + $100K 이상만
- **자동 실행:** GitHub Actions
- **호스팅:** GitHub Pages

## ⚠️ 일본 데이터 주의사항

일본은 한·미와 달리 **임원 개별 매수만 따로 추적하는 깔끔한 무료 데이터셋이 없어요.** 그래서 차선책으로 5% 이상 대주주의 보유 비율 증가(대량보유보고서)를 추적합니다.

- **잡히는 것:** 창업자·CEO가 5% 이상 들고 있다가 추가 매수, 행동주의 펀드의 신규 5% 진입, 자사주 매입에 가까운 대규모 매집
- **안 잡히는 것:** 임원이 1~2% 사는 경우, 사외이사의 소규모 매수
- **타이밍:** 신고 의무는 5%선 변동 후 5영업일 내 → "당일 매수"가 아니라 "최근 5%선 움직임"

신호 품질로 보면 한·미보다 노이즈가 적고 큰 그림 위주에요. 작은 매수까지 잡고 싶으면 J-Quants 유료 플랜이 필요합니다.

## 빠른 시작

### 1. GitHub 저장소 만들기

```bash
# 이 폴더 통째로 새 저장소에 푸시
gh repo create insider-tracker --public --source=. --push
# 또는 GitHub 웹에서 만들고 git push
```

### 2. API 키 발급

**DART API 키 (한국, 무료)**
1. https://opendart.fss.or.kr 접속 → 회원가입
2. 인증키 신청 (즉시 발급)
3. 발급된 40자리 키 복사

**SEC User-Agent (미국)**
- 별도 키 없음. SEC는 식별 가능한 user-agent 문자열만 요구
- 형식: `"Your Name your@email.com"` — 본인 이메일 사용

**EDINET API 키 (일본, 무료)**
1. https://api.edinet-fsa.go.jp 접속
2. 회원가입 → API 사용 신청 (메일 인증)
3. 발급된 Subscription Key 복사

### 3. GitHub Secrets 등록

저장소 → Settings → Secrets and variables → Actions → New repository secret

- `DART_API_KEY`: DART 키
- `SEC_USER_AGENT`: 예) `Hong Gildong gildong@example.com`
- `EDINET_API_KEY`: EDINET Subscription Key

### 4. GitHub Pages 활성화

저장소 → Settings → Pages
- Source: `Deploy from a branch`
- Branch: `main` / Folder: `/docs`
- Save

몇 분 뒤 `https://{username}.github.io/insider-tracker/` 에서 대시보드 접근 가능.

### 5. 워크플로 첫 실행

저장소 → Actions 탭 → "Daily Insider Buying Update" → Run workflow (수동 실행)

이후로는 매일 자동:
- **08:00 UTC (17:00 KST):** 한국 장 마감 후 데이터 수집
- **02:00 UTC (11:00 KST):** 전날 미국 장 Form 4 데이터 수집

## 로컬에서 테스트

```bash
pip install -r requirements.txt
export DART_API_KEY="발급받은_키"
export SEC_USER_AGENT="본인이름 이메일@example.com"
export EDINET_API_KEY="EDINET_subscription_key"

python scripts/build_data.py

# 결과 확인
cat docs/data.json

# 로컬에서 대시보드 보기
cd docs && python -m http.server 8000
# 브라우저에서 http://localhost:8000
```

## 필터 조정

기본 필터를 바꾸려면 각 스크립트 상단의 상수를 수정:

**`scripts/fetch_kr.py`**
```python
MIN_AMOUNT_KRW = 100_000_000  # 1억원 → 5억원으로 바꾸려면 500_000_000
OFFICER_TITLES = [...]  # 직책 목록 추가/제거
```

**`scripts/fetch_us.py`**
```python
MIN_AMOUNT_USD = 100_000  # $100K → $500K로 바꾸려면 500_000
PURCHASE_CODES = {"P"}  # 옵션 행사도 보려면 {"P", "M"} 추가
```

## 알려진 한계

1. **DART의 장내매수 vs 장외매수 분류** — 일부 공시는 거래사유 필드가 비어있거나 "기타"로 들어옴. 누락될 수 있음.
2. **시가총액** — 한국은 네이버 금융 페이지를 스크래핑함(API가 무료가 아님). 페이지 구조 바뀌면 깨질 수 있음. 깨지면 `fetch_kr.py`의 `fetch_market_cap` 정규식만 수정하면 됨.
3. **사업내용** — DART의 `induty`(업종) 필드 사용. 짧고 추상적임. 더 자세한 설명 원하면 회사 사업보고서 스크래핑 추가 필요.
4. **미국 Form 4 타이밍** — 거래 후 2영업일 내 신고 의무. 그래서 "오늘 거래"가 아니라 "오늘 신고된 것" 기준임. 거래일 기준으로 보려면 `fetch_us.py`의 `txn_date` 필터 추가.
5. **무료 호스팅의 한계** — GitHub Actions 무료 티어는 월 2,000분. 이 워크플로는 회당 ~5분 × 평일 2회 ≈ 월 200분이라 여유 있음.

## 파일 구조

```
insider-tracker/
├── docs/                    # GitHub Pages가 서빙하는 폴더
│   ├── index.html           # 대시보드 UI
│   └── data.json            # 매일 갱신되는 데이터
├── scripts/
│   ├── fetch_kr.py          # DART 수집기
│   ├── fetch_jp.py          # EDINET 수집기
│   ├── fetch_us.py          # EDGAR 수집기
│   └── build_data.py        # 셋을 합쳐 data.json 생성
├── .github/workflows/
│   └── update.yml           # cron 자동화
├── requirements.txt
└── README.md
```

## 면책

이 도구는 개인 리서치 목적이며, 투자 권유나 재무 자문이 아닙니다. 데이터는 공개 공시 기반이지만 정확성은 보장되지 않습니다. 투자 결정은 본인 책임.
