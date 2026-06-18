# AWS 배포 가이드 — Re:Action 백엔드 (한이음 클라우드 지원)

> 왜 AWS인가: 한이음 클라우드 지원은 **AWS / NCP / KT Cloud** 중 하나만 가능(사무국이 계정 발급).
> 우리 백엔드는 Docker 컨테이너라 어디든 동작 → 그 중 **AWS** 선택.
> 배포 절차(Render, 임시/대체)는 [`DEPLOY.md`](DEPLOY.md) 참고. 본 문서는 **한이음 AWS 계정 수령 후** 운영 배포 기준.

---

## 0. 한이음 클라우드 계정 흐름 (먼저 이해)

```
6/16~6/20  팀오피스 → 실습장비 > 클라우드 > 신청 (종류=AWS, 사용기간, 월별 금액, 사유)  ← ★지금 여기 (오늘 6/18, 6/20 마감)
   ↓        ※ 멘티 신청 후 멘토에게 직접 승인 요청 (자동 알림 X)
6/22       심의 결과 통지
+2~3 영업일  클라우드 제공 업체가 "사용 가능한 AWS 계정 정보" 개별 안내  ← 이때부터 배포 가능
```

- **정산**: 별도 정산 신청 없음. 신청금 한도 내 **실제 이용금액 자동 정산** (우리 카드 결제 아님).
- **제약**: 프로젝트당 **동시 1개 서버**. 90% 소진 시 사무국·업체와 협의.
- ⚠️ **이 계정 도착 전엔 AWS 배포 불가** → 그동안은 Render free 로 dev/데모 준비([`DEPLOY.md`](DEPLOY.md)).

---

## 1. 컴퓨트 선택 — **AWS Lightsail Container Service (권장)**

| 후보 | 적합도 | 메모 |
| --- | --- | --- |
| **Lightsail Containers** ✅ | **권장** | **flat 월정액**($7/$10/$20)이라 신청서 "월별 금액"과 정확히 맞음. HTTPS 엔드포인트·헬스체크 내장. Docker 이미지만 올리면 끝 — Render/App Runner와 가장 유사 |
| EC2 + Docker | 대안 | t3.small VPS 에 `docker run`. 더 싸고 자유롭지만 TLS(nginx/caddy)·운영 직접. 시간당 과금이라 월액 예측 약간 번거로움 |
| ECS Fargate | 비권장 | task def·ALB·VPC 설정 과함. 학생 데모엔 오버엔지니어링 |
| ~~App Runner~~ | **불가** | 2026-04-30 신규 고객 차단 |

→ **100명 베타 = small(0.5vCPU/2GB) 배포 권장** — 워크로드가 LLM(Gemini)·DB(Supabase) 외부 호출 위주라 **I/O 바운드** → small로 충분, micro는 아침/저녁 몰림 때 빠듯. cap은 small+여유로 **₩40k 신청**(천장일 뿐 실사용만 정산). 아래는 Lightsail Containers 기준(EC2 선호 시 §6).

## 2. 사전 준비 (계정 수령 후)

- 한이음이 준 AWS 계정/IAM 자격으로 **AWS CLI** 설정: `aws configure` (region: **ap-northeast-2 / 서울**)
- 로컬에 Docker, 그리고 `aws lightsail` (CLI v2 포함)

## 3. 배포 (Lightsail Containers)

```bash
# 1) runtime 이미지 빌드 (멀티스테이지 마지막 = prod uvicorn)
docker build --target runtime -t reaction-backend:latest .

# 2) 컨테이너 서비스 생성 (최초 1회) — small = $20/mo (100명 베타 권장), 서울 리전
aws lightsail create-container-service \
  --service-name reaction-backend \
  --power small --scale 1 --region ap-northeast-2
#   ※ 데모만이면 micro($10)도 충분. small($20)이 100명 베타 기준. medium($40)은 cap(₩40k) 초과 → 다음 창구에 cap 상향 재신청 후 전환

# 3) 이미지 푸시
aws lightsail push-container-image \
  --service-name reaction-backend --label app \
  --image reaction-backend:latest --region ap-northeast-2
#   → 출력의 이미지 참조(:reaction-backend.app.N)를 아래 containers.json 에 사용

# 4) 배포 (containers.json + public-endpoint.json 작성 후)
aws lightsail create-container-service-deployment \
  --service-name reaction-backend \
  --containers file://containers.json \
  --public-endpoint file://public-endpoint.json \
  --region ap-northeast-2
```

`containers.json` (시크릿은 §4):
```json
{
  "app": {
    "image": ":reaction-backend.app.1",
    "ports": { "8000": "HTTP" },
    "environment": {
      "APP_ENV": "prod",
      "AUTH_STUB_MODE": "false",
      "LLM_MODEL": "gemini-2.5-flash",
      "DATABASE_URL": "<Supabase Session pooler URL>",
      "JWT_SECRET": "<secrets.token_hex(32)>",
      "GEMINI_API_KEY": "<AI Studio prod key>",
      "COLUMN_ENCRYPTION_KEY": "<python -m reaction_backend.safety.encryption>",
      "GOOGLE_OAUTH_CLIENT_ID": "<FE와 동일>",
      "CORS_ALLOW_ORIGINS": "[\"https://reaction-frontend.vercel.app\"]",
      "CORS_ALLOW_ORIGIN_REGEX": "^https://reaction-frontend-.*\\.vercel\\.app$"
    }
  }
}
```
`public-endpoint.json` (헬스체크 → 우리 `/health`):
```json
{
  "containerName": "app",
  "containerPort": 8000,
  "healthCheck": { "path": "/health", "successCodes": "200" }
}
```

배포 후 Lightsail이 `https://reaction-backend.<id>.ap-northeast-2.cs.amazonlightsail.com` 형태 HTTPS URL 부여 → FE `VITE_API_BASE_URL` 에 연결.

## 4. 시크릿 (render.yaml 의 sync:false 와 동일 6종)

`DATABASE_URL` · `JWT_SECRET` · `GEMINI_API_KEY` · `COLUMN_ENCRYPTION_KEY` · `GOOGLE_OAUTH_CLIENT_ID` · `CORS_ALLOW_ORIGINS`
- 생성법은 [`DEPLOY.md`](DEPLOY.md) 표 그대로 (변경 없음).
- ⚠️ 평문 커밋 금지 — Lightsail 콘솔/`containers.json`(로컬, .gitignore)에만.

## 5. DB·마이그레이션·CORS

- **DB(Supabase) 그대로** — AWS로 옮기지 않음(외부 연결). `DATABASE_URL` 동일.
- **마이그레이션**: 기존 CD(`.github/workflows/migrate.yml`)가 Supabase 에 `alembic upgrade head` — 변경 없음.
- **CORS**: FE(Vercel) 도메인 유지.

## 6. (대안) EC2 + Docker

```bash
# t3.small (서울), Amazon Linux 2023, 보안그룹 80/443/22
sudo dnf install -y docker && sudo systemctl enable --now docker
docker build --target runtime -t reaction-backend:latest .
docker run -d -p 80:8000 --env-file .env --restart=always reaction-backend:latest
# TLS 는 caddy 한 줄 리버스프록시 권장 (Let's Encrypt 자동)
```
- 장점: 더 저렴·자유. 단점: TLS·업데이트 수동. flat 월액 예측이 Lightsail 보다 번거로움.

## 7. 비용 / 신청서 "월별 금액" 산정

| 옵션 | 월 비용 | 4개월(7~10월) |
| --- | --- | --- |
| Lightsail micro (1GB) | $10 ≈ ₩13.5k | ≈ ₩54k |
| Lightsail small (2GB) | $20 ≈ ₩27k | ≈ ₩108k |

→ **100명 베타 = small($20≈₩27k/월) 권장**. 신청서 월 금액(cap)은 small+환율·몰림 여유로 **₩40k**(천장일 뿐, 실사용만 정산). 4개월 실비 ~₩108k(cap ₩160k), 잔액 ₩1.05M 대비 여유 큼.

## 8. 한이음 클라우드 신청 — ✅ 제출 완료 (2026-06-19, 6월 2차)

**제출 내역**(신청 내역 화면 확인):
- **클라우드 종류**: AWS · **사용기간**: 7~8월(2개월) · **신청금액**: 70,000원 (월 35,000 × 2)
- **심의상태**: 신청(접수) · **멘토승인**: 미승인 → **멘토 직접 승인 요청 필요**(자동 알림 없음) · 심의 6/22
- 승인 시 영업일 2~3일 내 업체가 AWS 계정 개별 안내.
- **신청사유**(제출본): "한이음 창의도전 프로젝트 'Re:Action — 다시 시작하게 돕는 AI 실행 코치'(26_HA015)의 백엔드 API 서버 호스팅 신청입니다. FastAPI 기반 백엔드를 Docker 컨테이너로 AWS Lightsail Container(서울 리전)에 상시 배포하여, 프론트엔드(Vercel)·데이터베이스(Supabase)와 연동된 실서비스를 운영합니다. ... 우선 7~8월분을 신청하며, 운영 상황에 따라 이후 추가 신청 예정입니다."

**왜 2개월만**: 9~10월은 운영 불확실 + 미사용 cap 차액 소멸 가능성 → 확실한 7~8월만 우선 신청. 8월 창구(8/16~20)에서 9~10월 재신청 예정.
- **월 금액 ₩35k = cap**(천장). 실배포 Lightsail **small**($20≈₩27k) + 환율·몰림 여유. 한도 내 **실사용만 자동 정산**.
- ⚠️ 목록 안내에 **신청 기간이 4~9월**(월 1회 짝수 차시, 총 6회)로 표기됨 → **10월 클라우드 지원 여부 불확실**. 8월 재신청 때 사무국 확인 필요.
- ⚠️ 클라우드 문의(차액 소멸/환원, 계정 연속성, 10월 지원, Gemini SW)는 **[팀오피스 → 문의게시판 → 클라우드]** 로만 접수(업체 직접 답변).

## 9. ⚠️ LLM(Gemini)은 이 클라우드 지원 대상 아님

Gemini 는 Google(GCP)이라 AWS/NCP/KT 목록에 없음 → **클라우드 항목으로 미지원**. 별도 처리:
- 자비 결제, 또는 **SW(소프트웨어) 항목 지원 가능 여부 사무국 확인** (예산 [`BUDGET.md`](BUDGET.md) §4).
- (장기 옵션) AWS 위에서 운영 시 **Bedrock** 사용 시 LLM도 AWS 청구 → 지원 대상. 단 Gemini→Bedrock 은 provider/프롬프트 재작업이라 데모 후 검토.
