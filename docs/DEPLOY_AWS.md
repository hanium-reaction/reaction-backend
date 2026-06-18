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

→ 아래는 **Lightsail Containers** 기준. (EC2 선호 시 §6 참고)

## 2. 사전 준비 (계정 수령 후)

- 한이음이 준 AWS 계정/IAM 자격으로 **AWS CLI** 설정: `aws configure` (region: **ap-northeast-2 / 서울**)
- 로컬에 Docker, 그리고 `aws lightsail` (CLI v2 포함)

## 3. 배포 (Lightsail Containers)

```bash
# 1) runtime 이미지 빌드 (멀티스테이지 마지막 = prod uvicorn)
docker build --target runtime -t reaction-backend:latest .

# 2) 컨테이너 서비스 생성 (최초 1회) — micro = $10/mo, 서울 리전
aws lightsail create-container-service \
  --service-name reaction-backend \
  --power micro --scale 1 --region ap-northeast-2

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

→ 데모/베타엔 **micro($10)** 면 충분. 잔액(≈₩105만) 대비 여유 큼. 신청서엔 micro 기준 월 ₩13.5k × 개월수.

## 8. 한이음 신청서(6/16) 작성값 — 요약

- **클라우드 종류**: AWS
- **사용기간**: 7월 ~ 10월 (승인 6/22 이후 가동)
- **월별 금액**: [금액 생성] → Lightsail micro 기준 (~₩13.5k/월)
- **신청사유**: "Re:Action 백엔드(FastAPI/Docker) 상시 호스팅. Supabase 연동, 중간발표·베타 상시 가동 필요. Lightsail Container(서울) micro."
- 신청 후 **멘토 승인 요청** 필수.

## 9. ⚠️ LLM(Gemini)은 이 클라우드 지원 대상 아님

Gemini 는 Google(GCP)이라 AWS/NCP/KT 목록에 없음 → **클라우드 항목으로 미지원**. 별도 처리:
- 자비 결제, 또는 **SW(소프트웨어) 항목 지원 가능 여부 사무국 확인** (예산 [`BUDGET.md`](BUDGET.md) §4).
- (장기 옵션) AWS 위에서 운영 시 **Bedrock** 사용 시 LLM도 AWS 청구 → 지원 대상. 단 Gemini→Bedrock 은 provider/프롬프트 재작업이라 데모 후 검토.
