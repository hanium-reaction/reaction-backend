# 회복(Recovery) 에이전트 근거 대장 · 재설계안 v1

> 작성 2026-08-17 · 대상 독자: 팀 / 멘토 / 심사위원 / 포트폴리오
> 이 문서가 답하는 질문: **"왜 이걸 골라서 만들었는가"** — 회복 파이프라인의 모든 설계 결정에 문헌 근거를 붙이고, 근거가 없는 것은 없다고 표기한다.
> 짝 문서: [`docs/experiments/experiment-plan-v1.md`](../experiments/experiment-plan-v1.md) — 이 근거를 어떻게 검증할 것인가.

---

## 0. 요약 — 이 조사가 바꾼 것 5가지

| # | 발견 | 지금 우리 시스템 | 바꿔야 할 것 |
|---|---|---|---|
| 1 | 실패를 붕괴로 만드는 것은 **실패 자체가 아니라 실패에 붙는 귀인**이다. 자기귀인으로 스트릭이 끊긴 조건의 계속 참여율은 28.87%, 외부귀인은 42.00% (OR=0.56, p=.019) | 13태그 선택 UI가 이미 '상황 귀인 유도 장치'인데 **문서에도 코드에도 그 근거가 없다** | 태그 UI를 핵심 개입으로 승격해 문서화. 프롬프트의 "상황 탓" 규칙을 **완전 면책이 아닌 방향**으로 교정 |
| 2 | 실패 다음날 복귀율은 개입으로 **0.37 → 0.55** 까지 움직인다 (Sharif & Shu 2021, 비상 예비분 현장실험) | 이 축을 **재는 지표가 없다** | `next_day_return_rate` 신설 — 문헌과 직접 대조 가능한 유일한 외부 벤치마크 |
| 3 | 우리 대표 KPI `resilience_rate` = 회복 카드 **수락률**. 수락은 의도이지 행동이 아니다. 사용자 선호형 지표는 아첨(sycophancy)을 보상한다 | `weekly_review.py:196` — 가장 쉬운 카드(DOWNSCOPE)·가장 부담 없는 카드(PARK)를 최적화하면 KPI가 오른다 | 수락률은 **퍼널 상단**으로 강등, 대표 지표는 **실제 재실행 완주** 기반으로 이동 |
| 4 | 13태그 중 **TIME_SHORTAGE / OVERRUN / AVOIDANCE** 는 어떤 전략에도 매칭돼 있지 않다. 특히 AVOIDANCE 는 "정서조절 실패"라는 미루기의 핵심 기제인데 처방이 비어 있다 | 룰 엔진이 패딩으로만 카드를 채운다 | 신설 전략 4종 + 기존 2전략 태그 확장 |
| 5 | **연속 실패는 실패의 누적이 아니라 질적으로 다른 국면**(action crisis)이다. 같은 카드를 3일째 내미는 것은 반추를 강화한다 | 룰 엔진이 매 실패를 **독립 사건**으로 처리 | L0~L4 에스컬레이션 정책 신설 |

---

## 1. 조사 방법 (보고서 방법론 절에 그대로 쓸 것)

- **다중 에이전트 문헌 조사**: 6개 축(실패 직후 심리 / 실행의도·코핑플랜 / 연속성·재시작 / JITAI·알림 타이밍 / AI 코치·동기면담 / 실험설계) 병렬 조사.
- **적대적 인용 검증**: 각 축의 수집 결과를 **별도 검증 에이전트**가 받아 ① 논문 실재 여부 ② 저자·연도·게재지 일치 ③ 효과크기가 원문과 일치하는지 ④ 논문이 실제로 그 주장을 하는지를 재검색으로 판정. 판정값은 `CONFIRMED / CORRECTED / UNVERIFIABLE / FABRICATED`.
- **규모**: 에이전트 15개, 웹 검색·원문 조회 548회.
- **결과**: 수집 주장 **70건 중 FABRICATED 0건, CONFIRMED 61건, CORRECTED 9건.** 다만 효과크기 수준에서 `OVERSTATED / UNVERIFIABLE` 판정을 받은 항목이 **14건** 있었고, 그 14건은 전부 교정치로 대체하거나 인용을 금지했다.
- **왜 이 방법이 의미 있는가**: LLM 문헌 조사의 최대 실패 모드는 **그럴듯한 가짜 인용**이다. 생성과 검증을 분리하고 검증자에게 "의심스러우면 기본값은 UNVERIFIABLE"을 지시한 것이 이 조사의 방법론적 핵심이며, 실제로 9건의 서지 오류와 14건의 수치 왜곡을 잡아냈다.

### 1.1 인용 규칙 (이 문서 전체에 적용)

1. **FABRICATED / UNVERIFIABLE 판정 주장은 근거로 쓰지 않는다.** §9 "쓰지 않은 것"에 남긴다.
2. **OVERSTATED 판정 항목은 교정치로만 인용**하고, 판정 사실을 표에 남긴다.
3. **동료심사 미통과 문헌(프리프린트·워킹페이퍼)은 방향 참조로만 쓰고 수치를 단정 인용하지 않는다.** 표의 `검증` 열에 `미심사`로 표기한다. 이 규칙은 arXiv·NBER WP·Research Square에 **동일하게** 적용한다.
4. 문헌이 없는 결정은 **"근거 없음(설계자 판단)"** 이라고 쓴다. 얼버무리지 않는다.
5. 코드에서 확인한 사실은 문헌이 아니라 **레포 사실**로 표기한다.

---

## 2. 근거 대장

> 열 `검증`: 적대적 검증 결과. `단서`: 인용할 때 **반드시 함께 써야 하는** 한계.

### 2.1 실패 직후 — 왜 놓아버리고, 무엇이 되돌리는가

| ID | 문헌 | 이 설계에서 쓰는 범위 | 효과크기 | 검증 | 단서 |
|---|---|---|---|---|---|
| **A1** | Breines & Chen (2012). *Self-Compassion Increases Self-Improvement Motivation*. PSPB 38(9) 1133–1143. DOI 10.1177/0146167212445599 | 실패 직후 **자기자비**가 재시도 노력을 늘린다 | 실패 후 자율 학습시간 **306.5s**(자기자비) vs 229.9s(자존감) vs **203.2s**(무처치), F(2,83)=3.12, p<.05, ηp²=.07 | CONFIRMED | n≈27–30 소표본. **2차 시험 성적은 조건 간 무차이.** 자기자비 vs 자존감은 p=.085 한계적 |
| **A2** | Tangney, Stuewig & Martinez (2014). *Two Faces of Shame*. Psych Science 25(3) 799–805 | **수치심**은 외현화(남 탓) 경유로 재범을 정적 예측, **죄책감**은 직접·부적 예측 | N=476 → 1년 후 332 / 공식기록 446 | CONFIRMED | **수감자 코호트** — 일상 목표 실패로의 외적 타당도 제한. 관측 경로모형(인과 아님) |
| **A3** | Wrosch et al. (2003). *Adaptive Self-Regulation of Unattainable Goals*. PSPB 29(12) 1494–1508 | **이탈(disengagement)과 재관여(re-engagement)는 별개 역량** | 3개 연구 N=115 / 120 / 45 | CONFIRMED | 상관 설계. "포기는 적응적 절반"을 인과로 쓰지 말 것 |
| **A4** | Herrmann & Brandstätter (2015). *Action crises and goal disengagement*. Motivation Science 1(2) 121–136. DOI 10.1037/mot0000016 | **action crisis** = 실패 누적과 질적으로 다른 국면. ACRISS 6측면(갈등·반복좌절·실행방향상실·반추·포기충동·미루기) | 종단 예측 유의 | CONFIRMED (6측면 완전 일치) | 종단 관측, 계수 미확인 |
| **A5** | Larimer, Palmer & Marlatt (1999). *Relapse Prevention*. Alcohol Res Health 23(2) 151–160. PMC6760427 | lapse→relapse 를 매개하는 것은 **귀인** — 상황·특정 귀인이 보호적 | 개별 효과크기 미보고 | CONFIRMED | 원문은 RP 가 **금욕률 자체에서는 타 치료 대비 우위가 없다**고 명시. 반증(PMID 21787035): 첫 lapse 후 자책·자기효능감이 재발을 예측하지 못한 연구 존재 → **단순 인과 금지** |
| **A6** | Wohl, Pychyl & Bennett (2010). *I forgive myself, now I can study*. Pers Individ Dif 48(7) 803–808. DOI 10.1016/j.paid.2010.01.029 | **자기용서 → 이후 미루기 감소** (부정정서 매개) | N=119 | CONFIRMED | **1차 시험에서 심하게 미룬 하위집단의 조절 관계** — 무조건 일반화 금지 |
| **A7** | Sirois & Pychyl (2013). *Procrastination and the Priority of Short-Term Mood Regulation*. SPPC 7(2) 115–127 | 미루기 = 단기 기분 회복 실패 → **개입 표적은 착수 시점 정서** | 이론 리뷰, 효과크기 없음 | CONFIRMED | 리뷰는 임계값을 주지 않는다 — 수치 규칙의 근거로 쓰지 말 것 |
| **A8** | Prinsen et al. (2018). PSPB 44(6) 914–927 | 자기면죄는 **큰 실패 직후에만** 후속 실패를 줄인다(clean slate). 과도하면 부메랑 | 단순기울기 −.11 | CONFIRMED | 반론측 정확 서지 = de Witt Huberts, Evers & de Ridder (2014) PSPR 18(2) 119–138 |

### 2.2 계획 — if-then 은 얼마나, 어떤 조건에서 듣는가

| ID | 문헌 | 범위 | 효과크기 | 검증 | 단서 |
|---|---|---|---|---|---|
| **B1** | Gollwitzer & Sheeran (2006). AESP 38 69–119. DOI 10.1016/S0065-2601(06)38002-1 | if-then 형식 자체의 정당화 | **d = .65** (94 독립검정, N>8,000) | CONFIRMED | **자기 생성 계획**의 값이다. LLM 제안 수락에 그대로 전용하면 과장. 출판편향 미보정 → **상한선**으로만 |
| **B2** | Sniehotta, Scholz & Schwarzer (2006). BJHP 11 23–37 | action plan + **coping plan** > action plan only | N=211, 방향만 | CONFIRMED(방향) | 효과크기 미보고(원문 유료) |
| **B3** | Wang, Wang & Gai (2021). Front Psychol 12:565202 | MCII/WOOP 메타분석 | **g = 0.336** [0.229, 0.443]; 대면 .465 vs 문서 .277 | CONFIRMED | **MCII vs if-then 단독의 직접 비교는 없다** → WOOP 전면 도입 근거로 쓰지 말고 '장애물 명시' 요소만 차용 |
| **B4** | Sheeran, Webb & Gollwitzer (2005). PSPB 31 87–98 | 실행의도 효과는 **목표몰입 강도에 조절**된다 | 상호작용 유의 | CONFIRMED | "if-then 은 몰입을 만들지 못한다"는 넘겨짚기 — 조절효과까지만 |
| **B5** | Webb, Sheeran & Luszczynska (2009). BJSP 48(3) 507–523 | **습관(대항습관)이 강할수록 if-then 이 약해진다** | 상호작용 유의 | CONFIRMED | 실험실 조작 |
| **B6** | Buehler, Griffin & Ross (1994). JPSP 67(3) 366–381 | **계획 오류** — 자기 과제 완료시간을 체계적으로 과소추정, 과거 경험이 예측에 반영되지 않음 | 다중 연구 | CONFIRMED | — |
| **B7** | Kruger & Evans (2004). JESP 40(5) 586–598 | **unpacking → 추정 정확도 개선** | 추정 시간 유의 증가 | CONFIRMED | **"쪼개면 더 잘 끝낸다"는 원문 주장이 아니다** (추정 편향 축소) |
| **B8** | Adriaanse et al. (2011). Appetite 56(1) 183–193 | **촉진형 d=0.51 > 억제형 d=0.29**, 엄격 통제조건일수록 효과 감소 | 전체 d=0.43 [0.28, 0.57] | CONFIRMED | 실험 설계 경고로도 사용(§8-19) |
| **C8** | Masicampo & Baumeister (2011). JPSP 101(4) 667–683 | **계획을 세우면 미완결 목표의 인지적 활성이 유예된다** | 5개 연구, 계획 진지함이 매개 | CONFIRMED | 2011 소표본 사회심리, 재현 단서 필요 |

### 2.3 연속성 — 끊긴 뒤 돌아오게 하는 것

| ID | 문헌 | 범위 | 효과크기 | 검증 | 단서 |
|---|---|---|---|---|---|
| **C1** | Lally et al. (2010). EJSP 40(6) 998–1009 | **1회 결손은 자동성에 실질 영향 없음** | 결손 1회 자동성 **−0.29점(0–42 척도)**; 결손 후 재수행 +0.55 vs 무결손 3일 +0.79 (차이 비유의) | CONFIRMED | **66일은 모델 적합 39명 하위집단의 중앙값**(범위 18–254일, 84일 초과는 외삽). 결손 분석 N=67 → 검정력 부족. **'하루'에 한정**, 며칠로 확대 금지 |
| **C2** | Silverman & Barasch (2023). J Consumer Research 49(6) 1095–1117 | **끊긴 스트릭 표시 → 참여 하락**, 자기귀인이 증폭, **복구 가능 고지가 완충** | 자기귀인 **28.87%** vs 외부귀인 **42.00%** (OR=0.56, p=.019) / 온전 93.14% vs 끊김 68.66% vs **복구가능 끊김 85.20%** (OR=2.63, p<.001) | CONFIRMED | 온전 vs 외부귀인 차이 자체는 **p=.064 한계적**. MTurk 랩 |
| **C3** | Sharif & Shu (2021). OBHDP 163 17–29 | **비상 예비분(계획된 예외)** → 실패 다음날 복귀 | **0.37(Hard) → 0.55(Reserve-Weekly)**, β=−0.18, p<.001. Easy 0.44, Reserve-Monthly 0.48. **'보유'가 아니라 '적용'** 시에만 효과 | CONFIRMED (전문 대조) | 만보기 현장실험. 우리 벤치마크의 핵심 |
| **C4** | Beshears et al. (2021). Management Science 67(7) 4139–4171 | **경직 루틴(고정 시간창)이 유연 루틴보다 사후 지속에 불리** | N=2,508. 유연이 통제 대비 주간 1회↑ **+12%p**, 경직 대비 창 밖 방문 +7%p | CONFIRMED | 사후 11–40주에서 유의성 소멸 |
| **C5** | Dai, Milkman & Riis (2014). Management Science 60(10) 2563–2582 | **프레시 스타트 효과** — 시간적 랜드마크 후 열망 행동 증가 | 헬스장 방문: 새 주 **+33.4%**, 새 학기 +47.1%, 새 해 +11.6% / 목표계약 새 주 +62.9% | CONFIRMED (13개 백분율 축자 일치) | 아카이브 상관. **개시(initiation)만 입증, 지속은 미검증** |
| **C6** | Dai, Milkman & Riis (2015). Psych Science 26(12) 1927–1936 | 랜드마크 **라벨링**의 인과 검증 | **3.54배**(25.61% vs 7.23%, N=165), **6.57배**(28.57% vs 4.35%) | CONFIRMED / **OVERSTATED 교정** | 원 수집본이 배수를 축소 전사했음. 교정치로만 인용 |
| **C7** | Kivetz, Urminsky & Zheng (2006). JMR 43(1) 39–58 | goal-gradient / endowed progress — **진행도 리셋은 동력을 파괴** | 12칸(2칸 선지급) > 10칸 | CONFIRMED | 소비자 리워드 맥락 |
| **C9** | Aulagnon et al. (2025). NBER WP 34173 | 스트릭 강조 현장실험 — **끊김 후 이탈 증거 없음** | N=60,000 무작위. 성취 0.13–0.17 SD (suggestive) | CONFIRMED / CORRECTED · **미심사** | 워킹페이퍼. '+0.7%p'는 성취가 아니라 **엔드라인 응시 확률**이었음 |

### 2.4 타이밍 — 언제 찌르는가

| ID | 문헌 | 범위 | 효과크기 | 검증 | 단서 |
|---|---|---|---|---|---|
| **D1** | Nahum-Shani et al. (2018). Ann Behav Med 52(6) 446–462 | **JITAI 6 구성요소**: decision point / intervention option / tailoring variable / **decision rules** / proximal / distal. **'아무것도 하지 않음'을 1급 개입 옵션에 포함**할 것 | 개념 프레임워크 | CONFIRMED / CORRECTED(5→6) | — |
| **D2** | Klasnja et al. (2019). Ann Behav Med 53(6) 573–582 (HeartSteps MRT) | 맥락 맞춤 제안의 **근접 효과와 감쇠** | 30분 창 걸음수 **평균 +14%**(35보, p=.06), **1일차 +66%**(167보, p<.01). 좌식억제 제안은 효과 없음 | CONFIRMED | 등록 44, 분석 37. 후향 등록 |
| **D3** | Bell et al. (2023). JMIR mHealth 11:e38342 | 알림 → **1시간 내 앱 오픈 3.5배** (95% CI 2.91–4.25). **이탈 시점은 무변화** | 이미 참여 중이면 0.80배(비유의) | CONFIRMED | **근접 창 = 60분의 직접 근거** |
| **D4** | Antinyan et al. (2021). JEBO 191 752–764 | 리마인더 **주 1회 > 1회성 > 주 2회** | 방향만 | CONFIRMED(서지)/수치 미확인 | **%p 인용 금지.** 상한은 1이지 2가 아니다 |
| **D5** | Milkman et al. (2022). PNAS 119(6) e2115126119 | 문자 리마인더 메가스터디 | 평균 **+2.0%p**, 최고(간격 둔 2통 + 'waiting for you') **+2.9%p**. N=689,693, 22조건 | CONFIRMED | **독감** 백신. 22개 문구 간 격차 ≈0.9%p → **문구 개인화의 이득은 작다** |
| **D6** | Milkman et al. (2021). Nature 600 478–483 | 헬스클럽 메가스터디. **'놓친 운동 후 복귀'를 겨냥한 개입이 우수군** | 54개 중 45%가 주간 방문 +9~27%. 종료 후 잔존 8% | CONFIRMED / 일부 추론 | **"최고=27%"는 범위 상한과 붙여 읽은 추론** → 단정 금지 |
| **D7** | Rogers & Milkman (2016). Psych Science 27(7) 973–986 | **구별되는 물리적 단서와 연상된 리마인더**의 우위 | 실험1 74% vs 42% (+32%p) | CONFIRMED | 근거등급 정정: 임상 RCT 아님. **유일한 현장 실험은 p=.083** → 처방이 아니라 가설 |

### 2.5 AI 코치 — 무엇이 위험한가

| ID | 문헌 | 범위 | 효과크기 | 검증 | 단서 |
|---|---|---|---|---|---|
| **E1** | Michie et al. (2013). Ann Behav Med 46(1) 81–95 | **BCTTv1 93 BCT / 16 그룹** — 국제 보고 표준 | 26개 BCT 중 23개 adjusted κ ≥ 0.60 | CONFIRMED | **코드 번호는 2차 목록에서 재구성** → 게재 전 원문 재대조 필수(§4 경고) |
| **E2** | Kluger & DeNisi (1996). Psych Bulletin 119(2) 254–284 | 피드백 평균 d=.41이지만 **1/3 이상이 성과를 낮췄고**, 원인은 주의가 과제 → **자아 수준**으로 이동한 것 | 607 효과크기 / 23,663 관찰 | CONFIRMED | — |
| **E3** | Magill et al. (2018). JCCP 86(2) 140–157 | 동기면담 기술적 가설 — **sustain talk ↔ 나쁜 결과 r=.19** (p<.001) | 36 studies / N=3,025 | CONFIRMED | 원저는 change talk 경로(r=−.16)도 지지. **한쪽만 인용하면 왜곡.** sustain talk 는 '내담자 발화'이지 '버튼 클릭'이 아니다 — 우리 버튼 클릭에 전용하는 것은 **설계자 판단** |
| **E4** | Ng et al. (2012). Perspect Psychol Sci 7(4) 325–340 | 자율성 지지 → 욕구충족 → 자율적 동기 | 184 데이터셋 | CONFIRMED(서지)/r 미확인 | **"전달 방식의 독립 효과 경로"를 검정한 논문이 아니다** — 강화 금지 |
| **E5** | Sharma et al. (2023). arXiv:2310.13548 | RLHF 어시스턴트의 **아첨(sycophancy)** 경향 | 5개 어시스턴트 × 4과제 | CONFIRMED · **미심사** | 원문은 "likely driven in part" — '구조적 산물'로 단정 금지 |
| **E6** | Cheng et al. (2025). arXiv:2510.01395 | **아첨 AI 1회 상호작용으로 관계 복구 행동 의향이 감소하고 자기정당성이 증가** | 11개 모델에서 사용자 행동 긍정 **+50%**, 사전등록 N=1,604 | CONFIRMED · **미심사** | **아첨을 억제하면 만족도·재사용 의향은 떨어진다** → 두 지표 동시 추적 필요 |
| **E7** | Moore et al. (2025). FAccT '25, arXiv:2504.18412 | LLM 상담 대체 시 낙인·부적절 응답·망상 동조 | — | CONFIRMED · **미심사** | re:action 은 치료자가 아니므로 **안전 에스컬레이션 근거로만** |
| **F3** | Zheng et al. (2023). NeurIPS D&B, arXiv:2306.05685 | **LLM-as-judge**, GPT-4 ↔ 인간 일치 >80% | 인간-인간 일치와 동등 수준 | CONFIRMED · **미심사** | **영어 개방형 쌍대선호** 수치 — 한국어 회복 코칭 절대채점으로 전이 금지 |

### 2.6 근거 없음 — 정직하게 표기할 것

다음 항목은 **문헌 근거가 없다.** 보고서에서 "설계 결정이며 실증 근거는 확보하지 못했다"로 쓴다.

- NANO_STEP 의 **"5분"** 이라는 숫자 (직접 검증 연구는 n=10 준실험뿐, 결과 초록 미보고). Weick(1984) small wins 는 **이론 에세이이자 조직·사회정책 수준**이라 개인 습관 근거로 전용하면 넘겨짚기다.
- **4그룹(DOWNSCOPE/RESCHEDULE/CARRY_OVER/PARK) 분류 자체** — BCTTv1 16그룹과도, TDF domain 과도 대응하지 않는 자체 UX 분류다.
- **13태그 분류 체계** — COM-B / TDF 와의 공식 매핑 문헌 없음.
- quiet hours 를 22:30 으로 확장하는 안.
- 자율성 지지 3요소(이유 제공 / 관점 인정 / 선택 제공) — 검증 결과 **Ryan et al. 2008 에는 그 내용이 없다.** Deci et al. 1994 계열로 서지를 확정하기 전까지 인용 금지.

---

## 3. 재설계 — 회복 에이전트 상태 기계

```
S0 감지 ──▶ S1 정서 처리 ──▶ S2 귀인 ──▶ S3 에스컬레이션 게이트
 (21시 회고 /   (조건부          (13태그 +      ├─ L0~L2 ─▶ S4 전략 선택
  04:00 cron)    acknowledgment)   정서 1문항)    ├─ L3 ────▶ S3b 목표 재협상 3장
                                                 └─ L4 ────▶ S3c stand-down (카드 없음)
S4 전략 선택 ──▶ S5 if-then + coping plan (LLM v3) ──▶ S6 톤·구조 게이트
   (룰엔진 +                                              │ 위반 → 템플릿 강등
    정서축·이력축)                                        ▼
                                    S7 HITL(수락/수정/거절) ──▶ S8 배치 + 재관여 앵커
                                                              ──▶ S9 재알림(T0/T1/T2)
                                                              ──▶ S10 결과 기록 + 근접창 판정
```

| 단계 | 하는 일 | 붙는 근거 | 신설? | 난이도 |
|---|---|---|---|---|
| **S0 감지** | 21시 회고 · 04:00 abandoned cron. **만료를 failed 로 종결하지 않는 원칙 유지** | **A5** — 사용자가 말하지 않은 실패를 시스템이 확정하면 안정·내적 귀인으로 미끄러진다. `expire_reflections.py` 의 기존 주석이 이미 이 논리를 갖고 있다 → **출처만 붙이면 된다** | 기존 | — |
| **S1 정서 처리** | `acknowledgment` 1절을 **조건부** 생성: `completion_status='failed'` **AND** (overwhelm≥4 **OR** AVOIDANCE **OR** 연속실패≥2) | **A1** 자기자비 조건이 무처치보다 실패 후 실제 노력을 늘렸다(306.5s vs 203.2s). **A8** 효과는 '큰 실패'에 조절되므로 **partial_done 에는 붙이지 않는다** | 신설 | M |
| **S2 귀인** | 13태그 UI 를 **'상황 귀인 유도 장치'로 문서화** + 정서 1문항(`task_aversiveness` 1–5) | **A5** 상황·특정 귀인이 붕괴를 막는다. **A7** 13태그가 전부 구조적 사유에 편향돼 개입 표적인 '착수 시점 정서'를 못 잡는다 | 태그=기존 / 문항=신설 | M |
| **S3 에스컬레이션** | 연속 실패 카운터로 L0~L4 분기 | **A4** 연속 실패는 목표 이탈을 예측하는 별개 국면 | 신설 | M |
| **S4 전략 선택** | 기존 교집합 점수 + 정서축 가산 + 에스컬레이션 승격 | **A7 / A4 / B5** | 기존 함수 확장 | M |
| **S5 코핑 플랜** | `if_clause`/`then_clause` **+ `obstacle`/`coping_clause`** | **B2** action plan 만으로는 부족. **현 v2 프롬프트는 스스로를 '코핑 플랜'이라 부르면서 예시 3개가 전부 action plan 이다** | 신설 | M |
| **S6 톤·구조 게이트** | `banned_words` 뒤에 결정적 검증기 | **A2** 주어=사람 → shame 축. **E2** 자아 수준 피드백은 1/3에서 성과를 낮췄다 | 신설 | M |
| **S7 HITL** | 3버튼 유지. `edited` 를 '실패'가 아니라 자율성 신호로 재분류 | **E4** (약한 근거 + 설계자 판단으로 표기) | 재해석 | S |
| **S8 배치** | 기존 격자 규칙 유지 + **PARK/CARRY_OVER 수락 시 `re_engagement_anchor_at` 필수** | **A3** 재관여 장치가 지금 아예 없다. **C5/C6** 랜드마크가 착수를 촉발(개시 한정) | 신설 | M |
| **S9 재알림** | §6 — 기존 3클래스·주3건 예산 안에서 T0/T1/T2 | **D3**(60분) **D5**(간격 둔 2통) **D4**(빈도 상한) | 확장 | M~L |
| **S10 결과** | `completed/abandoned/pending` + 근접창 판정 | **D1** proximal outcome 은 '수락'이 아니라 '지정 창 내 행동' | 확장 | M |

---

## 4. 태그 구멍 메우기 · BCT 매핑

### 4.1 신설 전략 4종 + 태그 확장 2종

현재 시드(`alembic/versions/d09c105520b5_*.py`) 기준 **TIME_SHORTAGE / OVERRUN / AVOIDANCE** 는 어떤 `primary_trigger_tags` 에도 없다.
⚠️ `tests/test_recovery_catalog_sync.py::test_uncovered_tags_are_a_design_decision_not_a_gap` 가 이 사실을 **핀으로 고정**하고 있으므로, 전략을 추가하면 **핀의 방향을 반전**시켜야 한다.

| 전략 코드 | 그룹 | primary_trigger_tags | min_unit | 템플릿 방향 | 근거 |
|---|---|---|---|---|---|
| **TIMEBOX_REBUDGET** (신설) | RESCHEDULE | `["TIME_SHORTAGE","OVERRUN"]` | 15 | "이 카드는 실제로 평균 {p50}분 걸렸어요. 다음엔 {p50}분으로 잡아볼까요?" | **B6** — 완료시간은 체계적으로 과소추정되고 과거 경험이 반영되지 않는다. 우리는 `execution_events.actual_duration_minutes` 로 그 '과거 경험'을 **이미 갖고 있다** |
| **BUFFER_INSERT** (신설) | RESCHEDULE | `["OVERRUN"]` | 15 | "직전 일이 길어졌던 날이었어요. 다음 슬롯 앞에 15분 여유를 넣을까요?" | **B6** — OVERRUN 은 '이 카드'가 아니라 **선행 카드**의 계획 오류라 축소가 아니라 버퍼가 맞다 |
| **SELF_FORGIVENESS_NANO** (신설) | DOWNSCOPE | `["AVOIDANCE","HARD_TO_START"]` | 5 | 자기용서 1문장 + 최소 착수 if-then | **A6 + A1 + A7** |
| **GOAL_RECHECK** (신설) | PARK | `["AVOIDANCE","PRIORITY_SHIFT"]` | 0 | if-then 이 아니라 질문 하나: "이 목표, 지금도 하고 싶은 게 맞을까요?" | **B4** — 실행의도 효과는 목표몰입에 조절된다. 몰입 저하 신호에 if-then 을 붙이는 것은 기대효과가 없는 조건에 개입을 낭비하는 것 |
| ENVIRONMENT_SHIFT (태그 확장) | DOWNSCOPE | `+["AVOIDANCE"]` **단 L2 이상에서만** | 30 | 기존 | **B5** — 반복 AVOIDANCE 는 문구가 아니라 단서 수준에서 끊는다 |
| DOWNSCOPE_DEFAULT (태그 확장) | DOWNSCOPE | `+["TIME_SHORTAGE"]` | 15 | 기존 | **B8** — 촉진형(d=0.51) > 억제형(d=0.29) |

**COMEBACK 을 5번째 그룹으로 만들지 않는다.** **D6** 이 '놓친 뒤 복귀' 개입을 지지하지만 4그룹은 AGENTS.md §1 잠금이다. 대신 **선두 카드 문구 층의 `comeback_ack` 프리픽스**(연속실패≥2일 때만)로 얹는다 — 잠금 위반 0, 스키마 변경 0, A/B 처치 변수로도 쓸 수 있다.

### 4.2 전략 → BCT 매핑 (보고서 게재용)

> ⚠️ **게재 전 필수**: 아래 BCT 코드 번호는 원문 PDF 추출 실패로 **2차 목록에서 재구성**됐다. `bct-taxonomy.com` 또는 Michie 2013 원문 Table 로 **1건씩 재대조**할 것. 특히 **8.1 / 8.2 / 8.7, 12.1 / 12.3, 15.2 / 15.3**.

| 전략 | 그룹 | 주 BCT | 보조 | 촉진/억제 |
|---|---|---|---|---|
| NANO_STEP | DOWNSCOPE | 8.7 Graded tasks | 1.4 Action planning | 촉진 |
| DOWNSCOPE_DEFAULT | DOWNSCOPE | 8.7 Graded tasks | 1.5 Review behaviour goal(s) | 촉진 |
| ENVIRONMENT_SHIFT | DOWNSCOPE | 12.1 Restructuring physical environment | 12.3 Avoidance/reducing cue exposure | 억제 |
| CONTEXT_REWARMING | DOWNSCOPE | 8.1 Behavioural practice | 15.2 Mental rehearsal | 촉진 |
| RESCHEDULE_DEFAULT | RESCHEDULE | 1.4 Action planning | — | 촉진 |
| ACTIVE_RECOVERY | RESCHEDULE | 8.2 Behaviour substitution | 12.1 | 촉진 |
| CARRYOVER_DEFAULT | CARRY_OVER | 1.4 Action planning | 1.5 | 촉진 |
| FREEZE_SLOT | CARRY_OVER | 1.5 Review behaviour goal(s) | 1.7 Review outcome goal(s) | 중립 |
| PARK_DEFAULT | PARK | 1.5 Review behaviour goal(s) | 13.2 Framing/reframing | 중립 |
| **TIMEBOX_REBUDGET** | RESCHEDULE | 1.2 Problem solving | 2.2 Feedback on behaviour | 촉진 |
| **BUFFER_INSERT** | RESCHEDULE | 1.4 Action planning | 1.2 | 촉진 |
| **SELF_FORGIVENESS_NANO** | DOWNSCOPE | 13.2 Framing/reframing | 8.7 · 1.4 | 촉진 |
| **GOAL_RECHECK** | PARK | 1.5 Review behaviour goal(s) | 1.7 · 1.9 Commitment | 중립 |
| *(전략 아님)* 프롬프트 상황귀인 규칙 | 전 카드 | **4.3 Re-attribution** | 13.2 | — |
| *(전략 아님)* if-then 형식 강제 | 전 카드 | 1.4 Action planning | 1.1 Goal setting | — |
| *(신설)* coping clause 강제 | 전 카드 | 1.2 Problem solving | — | — |

**가장 중요한 자기발견**: 우리 시스템에서 **가장 강한 BCT 는 프롬프트의 상황귀인 규칙(4.3 Re-attribution)** 인데, 이것이 카탈로그에 전략으로 등록돼 있지 않아 통계·감사 대상에서 빠져 있다.

**커버리지 공백 (보고서에 그대로 실을 자기비판)**

| 미사용 BCT | 왜 비었나 | 채울 수 있나 |
|---|---|---|
| 2.2 Feedback / 2.3 Self-monitoring | 주간 리뷰가 백분율만 주고 **고위험 상황 요약을 안 준다** | `top_failure_contexts` 로 해소 (근거 **A5**) |
| 3.x Social support | 1인용 앱 — 구조적 공백 | MVP 범위 밖. 한계로 명시 |
| 10.x Reward | 보상 장치 없음 | **D6** 이 '복귀 보상'을 지지하나 4그룹 잠금과 충돌 → 문구 층으로만 |
| 15.1 Verbal persuasion | **의도적으로 비움** | **A1** — 자존감 부양 조건이 자기자비보다 약했다. **근거 있는 배제** |
| 15.3 Focus on past success | 비어 있음 | **C7** endowed progress 로 채울 수 있음 |

---

## 5. 연속 실패 에스컬레이션

### 5.1 상태 변수

| 카운터 | 증가 | 리셋 | **partial_done 취급** |
|---|---|---|---|
| `consecutive_failure_count` | `failed` 종결 시 | `done` / `over_done` 시 0 | **중립(동결) — 증가도 리셋도 하지 않는다** |
| `same_tag_failure_count` | 동일 (계보, tag_code) `failed` | 동일 | 동일 |
| `recovery_rejected_streak` | `rejected`/`skipped` | `accepted`/`edited` 시 0 | — |
| `recovery_abandoned_streak` | `recovery_result='abandoned'` | `completed` 시 0 | — |

> **왜 중립인가**: 초안은 "partial_done 도 리셋"이었으나, 그러면 **매일 조금씩만 하고 마는 사용자는 매일 회복 카드를 받으면서 카운터는 영원히 0** 이 되어 에스컬레이션의 사각지대가 된다. 그런데 그 사용자가 정확히 action crisis 궤적이다. 반대로 실패로 세면 **C1**(1회 결손 무해)과 충돌한다 → **동결**이 두 요구를 동시에 만족하는 유일한 값이다.
> **레포 제약**: 카운터를 `action_items` 원본 행에 올리면 "회복 결정으로 원본을 건드리지 않는다"(AGENTS.md §2)의 정신에 닿는다. **별도 집계 테이블 또는 파생 뷰**에 둔다.

### 5.2 레벨 정책

| 레벨 | 진입 | 무엇을 바꾸는가 | 근거 |
|---|---|---|---|
| **L0** | 첫 실패 | 현행 그대로 | **B1** |
| **L1 축소→분해** | 동일 카드 2회 연속 실패 또는 회복 1회 abandoned | `then_clause` 를 "전체를 15분만"(축소) 금지 → **"하위 단계 정확히 하나"**(분해). acknowledgment 활성화 | **B7**(unpacking은 추정 정확도를 높인다 — *완수율 상승은 원문 주장 아님*), **A8**('큰 실패' 조건이 여기서 처음 충족) |
| **L2 단서 전환** | 동일 태그 3회 연속 실패 | **문구 다듬기 중단.** ENVIRONMENT_SHIFT 선두 강제 + `previous_if_clauses` 재사용 금지(문자열 비교로 자동 검증 가능) | **B5** — 이 조건에서 문구만 바꾼 if-then 재제시는 문헌상 기대효과가 가장 낮다 |
| **L3 목표 재협상** | 동일 goal 4회 연속 실패 또는 회복 2회 연속 rejected | 4그룹 통상 카드 대신 **재협상 3장**: [목표 축소=DOWNSCOPE] / [기한 재설정=RESCHEDULE] / [일시 중단=PARK]. 수락 시 `re_engagement_anchor_at` 필수 | **A4 + A3** — 4그룹 잠금을 깨지 않고 구현된다 |
| **L4 stand-down** | L3 + overwhelm≥4 + 최근 7일 실패율 임계 초과 | **회복 카드를 제안하지 않는다.** "오늘은 카드를 안 낼게요" + 정적 휴식 안내. LLM 미사용 | **E7**(다른 경로가 없는 것이 안전 공백) + **D1**('아무것도 안 함'은 1급 옵션) |

**순서의 근거**: 축소 먼저(**B8** 촉진형 우위) → 분해(**B7**) → 단서 전환(**B5**) → 이월·보류(**A3**) → 재협상(**A4**). **역방향 금지**(재협상에서 NANO_STEP 으로 되돌리지 않음)는 *근거 없음(설계자 판단)*.

### 5.3 sustain talk 가드 — 근거 등급을 낮춰서 유지

최근 7일 DOWNSCOPE/PARK 수락 3회↑ → 해당 그룹 강등 + 문제해결형 1장 강제 포함.
⚠️ **이것은 E3 가 지지하는 조치가 아니다.** E3 의 sustain talk 는 상담 세션에서 **내담자가 산출한 발화**이고 r=.19 는 관측 상관이다. "시스템이 제시한 카드를 3번 눌렀다"는 것은 발화가 아니라 **선택지 노출에 대한 반응**이며, A3(이탈은 별개 역량)와도 부딪힌다.
→ **"설계자 판단 + 워크스루로 검증할 가설"로 격하**하고 임계값 3회는 로그로 재추정한다.

### 5.4 임계값·가중치의 출처 분류 (정직 표기)

| 값 | 출처 |
|---|---|
| 근접 창 **60분** | **문헌 (D3)** |
| 알림 주 상한 | **문헌 (D4)** — 다만 문헌이 지지하는 상한은 **1**이지 2가 아니다 |
| acknowledgment 조건(overwhelm≥4 / AVOIDANCE) | **로그에서 추정할 것** — A7 은 이론 리뷰라 임계값을 주지 않는다 |
| L1/L2/L3 진입선 (2회/3회/4회) | **설계자 판단** |
| sustain talk 가드 (7일 3회) | **설계자 판단** |
| 점수식 가중치 `w_affect`/`w_esc`/`w_hab` | **미정 — 골든셋 민감도 분석으로 결정** (값 없이 배포 불가) |
| acknowledgment 25자 | **설계자 판단** |

---

## 6. 재알림 정책 — 잠금 안에서

### 6.1 먼저 인정할 잠금 (레포 사실)

| 레포 사실 | 위치 | 귀결 |
|---|---|---|
| `NOTIFICATION_CLASSES = ("morning_brief","pre_card","evening_reflection")` + DB CHECK 제약 | `db/models/notification_send.py:29` | **새 알림 클래스는 만들 수 없다.** `push_gate.send_push()` 가 목록 밖 클래스에 `ValueError` |
| `PUSH_WEEKLY_BUDGET = 3` (전 클래스 합산 rolling 7일) | `safety/push_gate.py:45` | 신규 넛지가 예산을 먹으면 **그날 저녁 회고 푸시가 막히고**, 그러면 21시 회고가 안 열려 회복 카드 자체가 생성되지 않는다 |
| `notification_sends` 컬럼 = `user_id / notification_class / sent_at` 뿐 | 동 파일 | **대상 카드도, 열람 여부도 기록되지 않는다** → 근접 효과 측정 불가 |
| 회복 시점 = 21시 일괄 회고만 | AGENTS.md §1 | 실패 직후 자동 푸시 금지 |

### 6.2 3접점 (잠금 준수 버전)

| 접점 | 시각 | 채널 | 근거 |
|---|---|---|---|
| **T0** 블록 직전 | 시작 −7~−2분 (현행 스윕 창) | push · `pre_card` (기존) | **D2** 근접 효과는 30분 창에서 관측 |
| **T1** 블록 후 미체크 | 시작 **+20분** | **인앱 배지/인박스 — push 아님** | **D3** 근접 창 60분 안. push 로 하려면 새 클래스가 필요해 잠금 위반 → 인앱으로 우회 |
| **T2** 다음날 복귀 제안 | 다음날 morning_brief **안의 슬롯 1개** | push · `morning_brief` (기존 클래스 재사용) | **C5/C6** 새 하루는 랜드마크(개시 한정). **D6** '놓친 뒤 복귀' 겨냥. 밤에 같은 회계 기간 안에서 실패를 재확인시키지 않는다 |

**중단 조건 (JITAI 의 'provide nothing', D1)**: 최근 앱 세션 있음 → skip(**D3**, 0.80배는 비유의 → 가설) / 무응답 누적 → pause, 재개는 **월요일**(**C5** 새 주 랜드마크) / L4 stand-down → 전 접점 skip / **보내지 않은 것도 사유와 함께 로깅**(안 보낸 기록이 없으면 인과 분석이 성립하지 않는다).

⚠️ **선행 조건**: T1 억제 조건("최근 15분 내 앱 세션")과 무응답 카운터는 **현재 계산 불가능하다.** `users.last_active_at` 은 덮어쓰기라 이력이 없고 세션 테이블이 없다. → `notification_sends.target_action_item_id` + `opened_at`, 또는 `app_sessions` 테이블이 **먼저** 필요하다.

### 6.3 기대효과 캘리브레이션 (과대 목표 방지)

| 지표 | 문헌상 현실적 규모 | 출처 |
|---|---|---|
| 푸시 1건의 근접 참여 증가 | 상대 3.5배(1시간 앱 오픈) ~ 절대 **+2.0%p**(행동) | D3 / D5 |
| 근접 실행률 개선 목표 | **절대 3~10%p** — "2배 개선" 류 목표는 세우지 않는다 | D2 / D5 |
| 장기 리텐션 개선 | **기대하지 않는다** — 이탈 시점 무변화, 종료 후 잔존 8% | D3 / D6 |
| 문구 개인화의 이득 | 22개 넛지 중 최고−평균 격차 **≈0.9%p** | D5 |

> 마지막 행은 **LLM personalize 가 실패해 fallback 되어도 서비스 가치가 무너지지 않는다**는 우리 아키텍처의 방어 논거이기도 하다.

---

## 7. 지표 재정의

### 7.1 무엇이 잘못됐나

| 현재 | 문제 | 근거 |
|---|---|---|
| `resilience_rate` = 실패 중 회복 카드 **수락** 비율 (`weekly_review.py:196`) | 카드를 누른 순간을 성공으로 센다. **가장 쉬운 카드와 가장 부담 없는 카드를 최적화하면 오른다** — 현 KPI 는 아첨을 보상한다 | **E6** / **D1** |
| `consistency_days` = **최장 연속 일수** (`_longest_streak`) | 1회 결손을 0으로 만들어 all-or-nothing 붕괴를 시스템이 직접 제조한다 | **C1** / **C2** |
| `restart_success_rate`, `repeated_failure_count` | 컬럼은 있는데 **항상 NULL** | — |

### 7.2 대체 지표 — 그룹별로 성공 정의가 다르다

> ⚠️ **가장 중요한 함정**: `api/routes/recovery.py:92` 의 `_GROUP_TO_SOURCE` 는 **DOWNSCOPE 와 CARRY_OVER 만** 새 action_item 을 만든다. RESCHEDULE·PARK 는 파생 카드가 없어 `resulting_action_item_id` 가 NULL 이다.
> 따라서 "파생 카드 완주"를 대표 지표로 삼으면 **PARK/RESCHEDULE 수락은 항상 실패로 계산된다** — 수락률 편향을 고치려다 정반대 편향을 만든다. 게다가 L3 재협상 3장 중 2장이 RESCHEDULE·PARK 라서 **에스컬레이션이 잘 작동할수록 KPI 가 떨어진다.**

| 지표 | 정의 | 역할 |
|---|---|---|
| `recovery_acceptance_rate` | (기존 resilience_rate 개명) 수락·수정 / 제시된 회복 기회 | **퍼널 상단**. 대표에서 강등 |
| **`recovery_followthrough_rate`** | 그룹별 성공 정의로 계산한 **회복 완주율**:<br>· DOWNSCOPE/CARRY_OVER → 파생 카드(`resulting_action_item_id`)의 실행이 완주<br>· RESCHEDULE → 재배치된 블록의 실행 이벤트가 완주<br>· PARK → 앵커 이후 7일 내 같은 goal 계보 카드 완주 | **새 대표 지표** |
| `drop_after_accept` | acceptance − followthrough | "수락은 했는데 안 함" 갭. **발표 대표 그림** |
| **`next_day_return_rate`** | 실패한 날 중 **다음날 1개 이상 완주**한 날의 비율. 주 정의는 `failed` 만(벤치마크 비교 가능성), `partial_done` 포함은 민감도 | **C3 의 0.37 / 0.44 / 0.55 와 직접 대조 가능한 유일한 외부 벤치마크** |
| `re_engagement_rate` | PARK/CARRY_OVER 수락 중 앵커 이후 복귀 비율 | **A3** |
| `proximal_execution_rate_60m` | 알림 후 60분 내 해당 카드 실행 발생률 | **D3** — 단, `notification_sends.target_action_item_id` 신설이 **선행 조건** |
| `consistency_rolling14` | 최근 14일 중 `done/over_done/partial_done` 이 있는 날 수 / 14. **연속성 보너스 없음** | **C1 / C2** |
| `tone_gate_rejected_rate` | 톤 게이트 거부 / LLM 호출 | 프롬프트 회귀 감시 |

**양면 지표 경고**: **E6** 는 아첨을 억제하면 **만족도·재사용 의향이 떨어진다**는 것도 보여준다. 대표 지표를 행동으로 옮기면 단기 만족도가 내려갈 수 있으므로 **두 축을 동시에 추적**한다.

### 7.3 검증된 SQL (실제 스키마 기준)

> 실제 컬럼명 확인 완료: `execution_events(plan_start_at, actual_start_at, actual_end_at, actual_duration_minutes, completion_status)` · `recovery_attempts(recovery_option_group, recovery_strategy_type, resulting_action_item_id, user_decision, recovery_result)` · `execution_failure_tags(execution_id, tag_code)` · `recovery_strategy_catalog.primary_trigger_tags` 는 **JSONB**.

```sql
-- 1) 태그 커버리지 구멍 — JSONB 이므로 배열 연산자(&&)가 아니라 ? 를 쓴다
SELECT t.tag_code
FROM   failure_reason_tags t
WHERE  t.is_active
  AND  NOT EXISTS (
         SELECT 1 FROM recovery_strategy_catalog c
         WHERE c.is_active AND c.primary_trigger_tags ? t.tag_code
       );
-- 현재 결과: TIME_SHORTAGE, OVERRUN, AVOIDANCE (3행)
```

```sql
-- 2) 수락률 vs 완주율 갭 (그룹별 성공 정의 — 발표 대표 그림 F10 의 원천)
WITH failed_exec AS (
  SELECT e.id, e.user_id,
         (e.plan_start_at AT TIME ZONE 'Asia/Seoul')::date AS kst_date
  FROM   execution_events e
  WHERE  e.user_id = :user_id
    AND  e.completion_status = 'failed'
    AND  (e.plan_start_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :d0 AND :d1
),
accepted AS (
  SELECT DISTINCT ra.execution_id
  FROM   recovery_attempts ra
  WHERE  ra.execution_id IN (SELECT id FROM failed_exec)
    AND  ra.user_decision IN ('accepted','edited')      -- ADOPTED_DECISION_VALUES
),
followthrough AS (
  SELECT DISTINCT ra.execution_id
  FROM   recovery_attempts ra
  LEFT   JOIN action_items ai ON ai.id = ra.resulting_action_item_id
  LEFT   JOIN LATERAL (
           SELECT 1 FROM execution_events e2
           WHERE  e2.action_item_id = ai.id
             AND  e2.completion_status IN ('done','over_done')
           LIMIT  1
         ) hit ON TRUE
  WHERE  ra.execution_id IN (SELECT id FROM failed_exec)
    AND  ra.user_decision IN ('accepted','edited')
    AND  (
           -- 파생 카드가 있는 그룹: 그 카드가 완주됐는가
           (ra.recovery_option_group IN ('DOWNSCOPE','CARRY_OVER') AND hit IS NOT NULL)
           -- 파생 카드가 없는 그룹: recovery_result 로만 판정 (현 코드 제약)
           OR (ra.recovery_option_group IN ('RESCHEDULE','PARK')
               AND ra.recovery_result = 'completed')
         )
)
SELECT count(*)                                                            AS failure_n,
       round(count(*) FILTER (WHERE f.id IN (SELECT execution_id FROM accepted))::numeric
             / NULLIF(count(*),0), 4)                                      AS acceptance_rate,
       round(count(*) FILTER (WHERE f.id IN (SELECT execution_id FROM followthrough))::numeric
             / NULLIF(count(*),0), 4)                                      AS followthrough_rate
FROM   failed_exec f;
```

```sql
-- 3) next_day_return_rate — Sharif & Shu (0.37 / 0.44 / 0.55) 와 직접 대조
WITH fail_days AS (
  SELECT DISTINCT (plan_start_at AT TIME ZONE 'Asia/Seoul')::date AS d
  FROM   execution_events
  WHERE  user_id = :user_id AND completion_status = 'failed'
    AND  (plan_start_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :d0 AND :d1
),
win_days AS (
  SELECT DISTINCT (plan_start_at AT TIME ZONE 'Asia/Seoul')::date AS d
  FROM   execution_events
  WHERE  user_id = :user_id AND completion_status IN ('done','over_done')
)
SELECT round(count(*) FILTER (WHERE (f.d + 1) IN (SELECT d FROM win_days))::numeric
             / NULLIF(count(*),0), 4) AS next_day_return_rate
FROM   fail_days f;
```

```sql
-- 4) top_failure_contexts — BCT 2.3 Self-monitoring 을 채우는 쿼리 (근거 A5)
SELECT t.tag_code,
       count(*)                                            AS n,
       round(count(*)::numeric / sum(count(*)) OVER (), 4)  AS share,
       mode() WITHIN GROUP (
         ORDER BY extract(hour FROM (e.plan_start_at AT TIME ZONE 'Asia/Seoul'))
       )                                                    AS modal_hour_kst
FROM   execution_events       e
JOIN   execution_failure_tags t ON t.execution_id = e.id
WHERE  e.user_id = :user_id
  AND  e.completion_status IN ('failed','partial_done')
  AND  (e.plan_start_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :d0 - 27 AND :d1
GROUP  BY t.tag_code
ORDER  BY n DESC
LIMIT  3;
```

> **규칙**: 위 SQL 4종과 실험 계획서의 지표 SQL은 **사전등록 전에 테스트 DB 에서 실제로 실행**하고, 시드 데이터에 대한 **기댓값을 핀 테스트로 고정**한다. 빈 결과가 아니라 값으로 고정해야 가드가 실제로 작동한다.

---

## 8. 하지 말아야 할 것 (문헌상 역효과가 확인된 것)

| # | 하지 말 것 | 근거 | 지금 우리 코드에 |
|---|---|---|---|
| 1 | **`consistency` 를 '최장 연속일수'로 계산·표기** | **C1** 1회 결손은 −0.29/42점(비유의)인데 스트릭은 그걸 0으로 만든다. **C2** 자기귀인 끊김 28.87% | ✅ 있다 — `_longest_streak`. FE '연속 N일' 문구도 금지 |
| 2 | 끊긴 진행도를 0으로 리셋해 보여주기 | **C7** goal-gradient 는 '남은 거리'로 작동 — 리셋이 동력을 최대로 파괴 | FE 확인 필요 |
| 3 | 자아 수준(특성형) 백분율을 그대로 노출 | **E2** 피드백의 1/3 이상이 성과를 낮췄고 원인은 주의가 과제→자아로 이동 | ⚠️ 주간 리뷰가 %로 노출 |
| 4 | 자존감 부양 문구("원래 잘하잖아요") | **A1** 자존감 조건(229.9s) < 자기자비 조건(306.5s) *(p=.085 한계적)* | 프롬프트에 금지 규칙 없음 |
| 5 | **무조건적 상황 귀인 / 완전 면책**("당신 잘못이 아니에요") | **A2** 수치심은 외현화 경유로 나쁜 결과를 예측. **E6** 아첨 1회 노출로 복구 행동 의향 감소 | ✅ 있다 — **v2 프롬프트 30행이 정확히 이것을 지시한다** |
| 6 | **수락률을 대표 KPI 로 두기** | **E6** | ✅ 있다 — `resilience_rate` |
| 7 | 소폭 미달(partial_done)에 위로 문구 남발 | **A8** 효과는 '큰 실패'에 조절되고 과도한 정당화는 자기조절 유능감을 낮춘다(−.11) | 신규 위험 — v3 에서 조건부로 차단 |
| 8 | 연속 DOWNSCOPE/PARK 무제한 승인 | 설계자 판단(E3 는 직접 근거가 아님, §5.3) | 가드 없음 |
| 9 | **알림 빈도를 늘려 문제를 풀기** | **D4** 주 2회 < 주 1회. **D3** 알림은 근접 행동만 살리고 이탈 시점을 못 바꾼다 | 잠금(주≤3)이 이미 막고 있음 — **완화를 시도하지 말 것** |
| 10 | 연속 실패에 **문구만 다듬은 if-then 재제시** | **B5 / A4** | ✅ 있다 — 룰 엔진이 매 실패를 독립 사건으로 처리 |
| 11 | `then_clause` 를 "전체를 15분만"으로 허용 | **B7 / B6** | ✅ 있다 — v2 24행 "가장 작은 한 걸음" |
| 12 | abandoned 를 failed 로 종결 | **A5** | ❌ 이미 지키고 있다 — **유지** |
| 13 | 요일·시각 미세최적화("주말 12:30") | 원 주장이 검증에서 OVERSTATED — 주말-평일 차이 **P=.18 비유의**, 12:30 은 90% CI 사후 탐색 | 도입 금지 |
| 14 | quiet hours 22:30 확장 | **문헌 근거 없음** | 도입 금지(23:00 유지) |
| 15 | **LLM 에 전략 선택권을 주거나 자유 대화로 확장** | **E5 / E7** | ❌ 잘 하고 있다 — 범위를 '선두 카드 문구 personalize'로 좁힌 것은 **의도치 않게 잘 된 방어 설계**. **ADR 로 박제할 것** |
| 16 | "5분 규칙"을 문헌 근거로 표기 | 직접 검증은 n=10 준실험뿐. **F5(Weick)** 는 조직·사회정책 수준 이론 에세이 | 보고서에 "제품 결정, 실증 근거 빈약"으로 정직 표기 |
| 17 | "66일이면 습관 완성" 카피 | **C1** 66일은 **39명 하위집단 중앙값**(18–254일) | 온보딩 카피 주의 |
| 18 | "하루 이상 빠져도 괜찮다"로 확대 | **C1** 은 '3일 연속 직후 1회 결손'에 한정. 같은 논문이 인용한 Armitage(2005)는 **주 단위 lapse 가 부적 예측요인** | 카피는 **'하루'에 한정** |
| 19 | **"회복 카드 vs 아무것도 안 함"으로 효과 측정** | **B8** 통제조건이 부실할수록 효과가 부풀려진다(저자 명시) | 실험 대조군 = **카탈로그 템플릿(fallback 경로)** |
| 20 | 자기연민을 "기분 좋아지게 하는 위로"로 구현 | **A1** 조작은 ①공통 인간성 ②판단 없는 수용 ③증진적 신념 3요소였다 | v3 에 3요소로 명문화 |

---

## 9. 쓰지 않은 것 (정직 목록)

| 항목 | 왜 |
|---|---|
| "죄책감이 위반을 증폭시키는 순환"을 Cochran & Tesser(1996)에 귀속 | 챕터가 검증한 것은 목표 근접성·프레이밍의 수행 효과이지 죄책감 매개 실험이 아니다. preload/disinhibition 은 **별개 계보**(Herman & Mack 1975 / Polivy & Herman 1985) |
| Prinsen 의 "같은 상황에서는 재관여가 안 되고 새 상황에서는 된다" | **출처가 아예 없다** |
| Liao et al. 2016 을 "적은 참가자로 MRT 가능"의 근거로 | **원문 주장이 아니다.** 검정력은 가용성 확률·근접 효과의 시간 형태에 좌우되고 소표본 F분포 보정 때문에 N 하한이 있다 |
| Ryan et al. 2008 을 자율성 지지 3요소의 출처로 | PDF 전문 확인 결과 **그 내용이 없다** |
| SCRIBE 26문항의 절별 배분 | UNVERIFIABLE. "26문항"까지만 사용 |
| Bidargaddi 의 "주말 12:30 최적" 실무 결론 | OVERSTATED — 주말-평일 차이 P=.18 비유의 |
| Duolingo 의 "streak freeze 장기 리텐션 +10%", "이탈 21% 감소", "7일 도달 2.4배" | 전부 3차 마케팅 블로그. 1차 자료 미확인 → **사용 금지**. 확인 가능한 것은 BRB 200만+ 스트릭 보호, '유예 2개 > 1개, 3개는 무이득' 뿐이며 표본·설계 비공개 |
| 자기연민-동기 메타분석 (45편, N=13,558, r=0.25) | Research Square 프리프린트 → §1.1-3 규칙 적용 |
| **스트릭 카운터 도입** | **C2(랩)와 C9(현장 N=60,000)가 서로 어긋난다** — 랩은 끊김 후 이탈, 현장은 이탈 증거 없음. 이 불일치는 스트릭 포기의 근거가 아니라 **두 조절변수(외부 귀인·복구 가능성)를 내장하라는 근거**다. 도입한다면 유연형만. 현 스코프에서는 §8-1(연속 스트릭 제거)만 먼저 한다 |
| 주간 비상 스킵 예산 즉시 도입 | **C3** 근거는 강하나(0.37→0.55) 사실상 5번째 경로 → 4그룹 잠금과 충돌. §10 합의 항목 |

---

## 10. PR 전 팀 합의가 필요한 항목

| # | 항목 | 충돌 잠금 | 합의 실패 시 대안 |
|---|---|---|---|
| 1 | 블록 후 넛지를 **push** 로 (T1) | 알림 3클래스·주≤3건 | **인앱 배지로 대체** (기본안) |
| 2 | 주간 비상 스킵 예산 (C3 근거) | 회복 옵션 4그룹 | 보류. `partial_done` 을 adherence 에 가중 반영하는 것만 먼저 |
| 3 | COMEBACK 5번째 그룹 | 4그룹 잠금 | **문구 프리픽스로 대체** (기본안) |
| 4 | `resilience_rate` 개명 | ADR-0002 응답 계약 동결 | 병행 노출 → 2단계 마이그레이션 |
| 5 | 회고에 `task_aversiveness` 1문항 | UX 부담 | 문항 없이 `overwhelm_level` 만으로 정서축 근사 |
| 6 | **LLM 심판을 다른 provider 로** (실험용) | AGENTS.md §8 새 외부 의존성 | 같은 provider 의 다른 모델 + "self-enhancement bias 완전 배제 불가"를 한계로 명시 |
| 7 | BCT 매핑표 보고서 게재 | — | **코드 번호 원문 재대조 전 게재 금지** |
| 8 | **LLM timeout 8초 vs 12초** | `config.py:109` 는 `8.0` 이고 주석에 **"ADR-0003 §1 동결값"**, `api/routes/recovery.py:247` 은 `timeout=12.0` 하드코딩 | **ADR 로 하나로 확정**해야 지연 실험의 기준선이 성립한다 |

---

## 부록 A. 전체 근거 원자료

70건 전체(주장·효과크기·검증 메모·re:action 매핑)는 조사 산출물에 보관되어 있다. 이 문서는 그중 설계에 실제로 쓰이는 **35건**만 대장에 올렸다. 보고서 집필 시 인용이 더 필요하면 원자료에서 가져오되, **§1.1 인용 규칙을 동일하게 적용**할 것.
