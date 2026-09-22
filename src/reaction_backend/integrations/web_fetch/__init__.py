"""사용자가 준 링크를 열어 본문을 가져오는 통합 (BE #226 1단계).

```python
result = await fetcher.fetch_text(url)   # 예외 없음 — 실패는 result.reason
if result.ok:
    ...
```

SSRF 판정은 `url_guard`, 판정을 통과한 IP 로만 접속하는 건 `pinned_http`, HTML→텍스트는
`extract`. 규격은 `tests/test_web_fetch.py` 가 고정한다.
"""
