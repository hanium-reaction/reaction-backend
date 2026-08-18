# `content/` — 인박스 추천 자료

이 디렉터리의 Markdown은 목표 카테고리에 맞춰 인박스에 추천되는 사용자용 자료다. 프롬프트가
아니라 API가 본문을 전달하고 화면이 렌더하는 최종 콘텐츠이므로 문구·링크·형식 변경은 제품
출력 변경으로 취급한다.

## 구조와 현재 자료

```text
content/<category>/<slug>.md
```

[`registry.py`](registry.py)가 디렉터리를 스캔해 `slug -> ContentDoc` 레지스트리를 만든다.
지원 카테고리는 목표 카테고리와 같은 9종이다.

`study`, `project`, `health`, `routine`, `schedule`, `career`, `relationship`, `self_dev`, `other`

현재 10개 자료가 있으며 9개 카테고리가 모두 채워져 있다. `health`에만 2개가 있고 나머지
카테고리에는 각 1개가 있다. 자동 인박스 삽입은
[`orchestrator/inbox_resources.py`](../orchestrator/inbox_resources.py)의
`_MAX_PER_CATEGORY = 1` 제한 때문에 카테고리별 slug 정렬상 첫 자료만 선택한다. 따라서 현재
자동 경로로 선택되는 자료는 9개이며, 나머지 자료도 slug를 알면 resource 조회 API로 열 수
있다.

## 레지스트리 API

- `get(slug) -> ContentDoc`: 전역 유일 slug로 한 건을 조회하고 없으면 `ContentNotFound`를
  발생시킨다. 입력 slug를 파일 경로와 결합하지 않는다.
- `list_all() -> list[ContentDoc]`: 카테고리와 slug 순으로 정렬한 새 목록을 반환한다.
- `list_by_category(category) -> list[ContentDoc]`: 해당 카테고리 자료를 slug 순으로 반환한다.
- `reload()`: 파일 변경 후 캐시를 비운다. 주로 테스트와 개발 중 hot reload에 사용한다.
- `parse_document()`과 `parse_steps()`: 정해진 frontmatter와 단계 목록을 검증한다.

스캔 중 잘못된 폴더·파일·frontmatter는 경고를 남기고 건너뛴다. 심볼릭 링크와 콘텐츠 트리
밖으로 해석되는 경로도 등록하지 않는다. 한 파일의 오류로 앱 전체 import가 실패하지 않게
한 대신, 커밋된 파일이 실제로 등록되는지는 테스트가 강제한다.

## 파일명과 frontmatter

`<slug>`는 파일명에서 `.md`를 뺀 값이며 `^[a-z0-9][a-z0-9-]{2,47}$`를 만족해야 한다.
API 경로 값이자 카테고리 전체에서 유일한 키다. 프롬프트 레지스트리와 달리 버전 접미사와
`latest` 승격 규칙은 없다. 내용을 바꾸려면 해당 파일을 수정한다.

파일 첫 부분에는 평평한 `key: value` 5개만 둔다.

```markdown
---
slug: exercise-plan-that-bends
title: 빠지는 날까지 계획에 넣기
category: health
summary: 정해둔 횟수를 '꼭 한 번'과 '되면 더'로 나눠 두면 덜 흔들려요.
steps: 이번 주 '꼭 한 번'을 정해서 적기 | 운동을 이미 하는 행동 뒤에 붙이기
---

# 빠지는 날까지 계획에 넣기
```

- `slug`, `title`, `category`, `summary`, `steps`는 모두 필수이며 빈 값일 수 없다.
- 중첩 YAML, 리스트, 멀티라인 값, 따옴표 해석, 주석은 지원하지 않는다.
- `slug`는 파일명, `category`는 상위 폴더명과 문자 단위로 같아야 한다.
- `title`은 본문의 첫 `# ` 제목과 정확히 같아야 한다. 화면은 이 일치를 기준으로 중복 H1을
  제거한다.
- API의 `markdown`에는 frontmatter를 제거한 본문만 들어간다.

길이 예산은 모바일 화면을 기준으로 제목 16자 이하, 요약 45자 이하, 본문 750~1,400자다.
정확한 검사값은 [`tests/test_content_registry.py`](../../../tests/test_content_registry.py)가
고정한다.

## `steps` 계약

`steps`는 자료에서 오늘 할 일로 채택할 수 있는 실행 단위다. `|`로 나누는 한 줄 형식이며
1~5개, 각 40자 이하, 중복 없음이어야 한다. 선택된 항목은 그대로 `ActionItem.title`이 되므로
오늘 바로 할 수 있는 짧은 명사형 문구로 작성한다. `|` 자체는 구분자이므로 단계 텍스트에
사용할 수 없다.

형식이 잘못되면 그 자료 전체가 레지스트리에서 제외된다. 사용자 채택은
`POST /inbox/{id}/adopt-step` 흐름을 거치며 자동으로 실행 카드가 적용되는 구조가 아니다.

## Markdown과 문체 규약

화면 렌더러가 지원하는 제목 1~3단계, 문단, 목록, 강조, 인용, 구분선, 링크, 작은 GFM 표,
인라인 코드를 사용한다. 다음 형식은 사용하지 않는다.

- raw HTML과 HTML entity
- 4단계 이하 제목, 체크박스, 각주, 코드 펜스
- 이미지와 외부 URL
- 앱의 내부 enum·전략 코드

자료는 한국어 존댓말 권유형으로 쓰고 원인을 사람의 의지나 성격이 아니라 상황과 계획 크기에
둔다. 연속 달성·성과 압박, 자기비난, 의학적 처방, 자동 적용을 암시하는 표현을 피한다.
금지어 단일 진실 소스는 [`safety/banned_words.py`](../safety/banned_words.py)다. 검사 통과는
필요조건일 뿐이므로 최종 문맥은 사람이 검토한다.

## 새 자료를 추가하는 절차

1. 지원 카테고리 폴더에 slug 규칙을 만족하는 UTF-8 Markdown을 만든다.
2. frontmatter 5개 값과 본문 H1을 일치시킨다.
3. [`tests/test_content_registry.py`](../../../tests/test_content_registry.py)의 `EXPECTED_DOCS`에
   `(category, slug)`를 추가한다.
4. 자동 삽입이 필요한지 확인한다. 카테고리당 한 건 제한 아래에서는 새 파일이 자동 선택되지
   않을 수 있다.
5. 콘텐츠와 인박스 경로 테스트를 실행하고 모바일 화면에서 길이와 제목 중복을 확인한다.

```bash
uv run pytest -v tests/test_content_registry.py tests/test_inbox_resources.py
```

## 제약

- 레지스트리는 프로세스 내 캐시를 사용하므로 실행 중 파일을 바꿨다면 `reload()` 또는 앱
  재시작이 필요하다.
- 외부 링크와 이미지를 허용하지 않아 출처를 외부 페이지로 연결하는 자료 형식은 현재 계약에
  맞지 않는다.
- 카테고리별 자동 선택 상한이 1이므로 자료 수를 늘리는 것과 자동 추천 다양성을 늘리는 것은
  같은 변경이 아니다.
