"""prompt_playground — 브라우저에서 직접 답을 입력하며 인터뷰·계획 프롬프트를 테스트.

DB/인증/프론트 레포 없이, 오케스트레이터(`interview_runner` + `first_plan`)를 그대로 구동한다
(`session=None` → budget/llm_runs/DB 우회). 즉 여기서 보이는 질문·요약·계획 = 실제 프롬프트가
사용자에게 내는 결과. `.env` 의 `GEMINI_API_KEY` 가 있으면 실 Gemini, 없으면 룰 fallback.

실행 (repo 루트):

    uv run python scripts/prompt_playground.py           # http://localhost:5173
    uv run python scripts/prompt_playground.py --port 8100

인터뷰 상태는 서버 프로세스 메모리에 세션별로 보관한다(단일 사용자 로컬 도구). 계획 패널의
thinking 예산 토글로 P1-3(계획만 thinking on)·P0-2(replan 피드백) 효과를 눈으로 비교할 수 있다.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from reaction_backend.api.mock.interview import SLOT_CATALOG
from reaction_backend.orchestrator import (
    first_plan,
    interview_adapter,
    interview_runner,
)
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.schemas.common import now_kst

_CATALOG_BY_KEY = {s.slot_key: s for s in SLOT_CATALOG}
_REQUIRED = interview_adapter.REQUIRED_SLOT_KEYS
_TONE = "gentle"

# 세션 상태 in-memory (로컬 단일 사용자 도구) — {session_id: InterviewState}
_SESSIONS: dict[str, InterviewState] = {}

app = FastAPI(title="re:action prompt playground")


# ─────────────────────────── 슬롯→질문 매핑 (라우터 로직 미러) ───────────────────────────
def _question_options(slot_key: str, slot_answers: dict[str, Any]) -> list[str]:
    """chip/select 보기. goals.heaviest 는 goals.list 응답에서 동적 생성 (라우터와 동일)."""
    if slot_key == "goals.heaviest":
        goals = slot_answers.get("goals.list")
        if isinstance(goals, dict) and goals.get("type") == "text":
            norm = goals.get("normalized")
            if isinstance(norm, list):
                return [str(x) for x in norm if str(x).strip()]
            raw = goals.get("raw")
            if isinstance(raw, str) and raw.strip():
                return [raw.strip()]
        return []
    slot = _CATALOG_BY_KEY.get(slot_key)
    return list(slot.options) if slot else []


def _slot_meta(slot_answers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        s.slot_key: {
            "label": s.label,
            "answer_type": s.answer_type,
            "options": _question_options(s.slot_key, slot_answers),
        }
        for s in SLOT_CATALOG
    }


def _remaining_required(slot_answers: dict[str, Any]) -> int:
    return sum(1 for k in _REQUIRED if not interview_adapter.is_filled_answer(slot_answers.get(k)))


def _question_payload(state: InterviewState) -> dict[str, Any] | None:
    slot_key = state["next_slot_key"]
    nq = state["next_question"]
    if nq is None or not slot_key:
        return None
    slot = _CATALOG_BY_KEY.get(slot_key)
    options = _question_options(slot_key, state["slot_answers"])
    answer_type = slot.answer_type if slot else "text"
    return {
        "slotKey": slot_key,
        "text": nq.question,
        "empathy": nq.empathy_one_liner,
        "answerType": answer_type,
        "options": options,
        "suggestedAnswers": [] if options else list(nq.suggested_answers),
        "remaining": _remaining_required(state["slot_answers"]),
    }


def _summary_payload(result: interview_runner.TurnResult) -> dict[str, Any]:
    s = result.summary
    o = result.outcome
    return {
        "done": True,
        "endReason": result.end_reason,
        "summary": s.model_dump(mode="json") if s is not None else None,
        "outcome": o.model_dump(by_alias=True, mode="json") if o is not None else None,
    }


# ─────────────────────────── API ───────────────────────────
class AnswerBody(BaseModel):
    sessionId: str
    value: Any


class FinishBody(BaseModel):
    sessionId: str


class PlanBody(BaseModel):
    sessionId: str
    thinkingBudget: int = 2048


@app.post("/api/start")
async def api_start() -> JSONResponse:
    sid = str(uuid4())
    result = await interview_runner.start_interview(
        session_id=uuid4(),
        user_id=uuid4(),
        session=None,
        tone_mode=_TONE,
        slot_meta=_slot_meta({}),
    )
    _SESSIONS[sid] = result.state
    return JSONResponse({"sessionId": sid, "question": _question_payload(result.state)})


@app.post("/api/answer")
async def api_answer(body: AnswerBody) -> JSONResponse:
    state = _SESSIONS.get(body.sessionId)
    if state is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    slot_key = state["next_slot_key"] or ""
    answered = _CATALOG_BY_KEY.get(slot_key)
    result = await interview_runner.submit_and_advance(
        state=state,
        slot_key=slot_key,
        answer_value=body.value,
        session=None,
        tone_mode=_TONE,
        answer_type=answered.answer_type if answered else None,
        options=_question_options(slot_key, state["slot_answers"]),
        slot_meta=_slot_meta(state["slot_answers"]),
    )
    _SESSIONS[body.sessionId] = result.state
    harvested = [
        {"slotKey": k, "label": (_CATALOG_BY_KEY[k].label if k in _CATALOG_BY_KEY else k)}
        for k in result.harvested
    ]
    if result.done:
        return JSONResponse({**_summary_payload(result), "harvested": harvested})
    return JSONResponse(
        {"done": False, "question": _question_payload(result.state), "harvested": harvested}
    )


@app.post("/api/finish")
async def api_finish(body: FinishBody) -> JSONResponse:
    state = _SESSIONS.get(body.sessionId)
    if state is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    result = await interview_runner.finish_early(state=state, session=None, tone_mode=_TONE)
    _SESSIONS[body.sessionId] = result.state
    return JSONResponse(_summary_payload(result))


@app.post("/api/plan")
async def api_plan(body: PlanBody) -> JSONResponse:
    state = _SESSIONS.get(body.sessionId)
    if state is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)

    # UI 토글값으로 계획 thinking 예산을 오버라이드 (P1-3 A/B).
    from reaction_backend.config import get_settings

    os.environ["LLM_PLANNING_THINKING_BUDGET"] = str(max(0, body.thinkingBudget))
    get_settings.cache_clear()

    outcome = interview_adapter.build_outcome(
        session_id=body.sessionId,
        slot_answers=state["slot_answers"],
        ambiguity_final=state["ambiguity_score"],
        end_reason="completed",
        analysis_source="llm",
    )
    fp_state = first_plan.initial_state(
        user_id=uuid4(), outcome=outcome, target_date=now_kst().date().isoformat()
    )
    config: Any = {"configurable": {"session": None, "tone_mode": _TONE}}

    started = time.monotonic()
    graph = first_plan.build_first_plan_graph()
    final = await graph.ainvoke(fp_state, config=config)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    gp = final["goal_plan"]
    review = final["review"]
    return JSONResponse(
        {
            "elapsedMs": elapsed_ms,
            "thinkingBudget": body.thinkingBudget,
            "usedFallback": final["used_fallback"],
            "aiSource": "rule" if final["used_fallback"] else "llm",
            "goalNodes": [n.model_dump(mode="json") for n in gp.goal_nodes] if gp else [],
            "actionItems": [a.model_dump(mode="json") for a in gp.action_items] if gp else [],
            "policyViolations": (
                [p.model_dump(mode="json") for p in gp.policy_violations] if gp else []
            ),
            "scheduledBlocks": [b.model_dump(mode="json") for b in final["scheduled_blocks"]],
            "warnings": final["schedule_warnings"],
            "review": review.model_dump(mode="json") if review is not None else None,
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    from reaction_backend.config import get_settings

    has_key = bool(get_settings().gemini_api_key)
    return HTMLResponse(_PAGE.replace("__HAS_KEY__", "true" if has_key else "false"))


# ─────────────────────────── UI (self-contained) ───────────────────────────
_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>re:action prompt playground</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#9aa4b2;
          --accent:#6ea8fe; --me:#2b3040; --ai:#1d2430; --warn:#f0b657; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.55 system-ui,'Segoe UI',sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:12px 18px; border-bottom:1px solid var(--line); display:flex; gap:12px; align-items:center; }
  header b { font-size:15px; } .tag { font-size:12px; color:var(--dim); }
  .key-ok { color:#69db7c; } .key-no { color:var(--warn); }
  main { display:grid; grid-template-columns:1fr 1fr; gap:0; height:calc(100vh - 50px); }
  @media (max-width:820px){ main{ grid-template-columns:1fr; height:auto; } }
  .col { display:flex; flex-direction:column; min-height:0; border-right:1px solid var(--line); }
  .col h2 { margin:0; padding:10px 16px; font-size:13px; color:var(--dim); border-bottom:1px solid var(--line);
            text-transform:uppercase; letter-spacing:.04em; display:flex; justify-content:space-between; }
  #chat { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:82%; padding:9px 12px; border-radius:12px; white-space:pre-wrap; }
  .ai { align-self:flex-start; background:var(--ai); border:1px solid var(--line); }
  .me { align-self:flex-end; background:var(--me); }
  .empathy { color:var(--dim); font-size:12px; margin-top:4px; }
  .sys { align-self:center; color:var(--dim); font-size:12px; }
  #composer { border-top:1px solid var(--line); padding:12px 16px; display:flex; flex-direction:column; gap:8px; }
  .opts { display:flex; flex-wrap:wrap; gap:6px; }
  button { font:inherit; cursor:pointer; border-radius:8px; border:1px solid var(--line);
           background:var(--panel); color:var(--fg); padding:7px 12px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#0b0e14; border-color:var(--accent); font-weight:600; }
  button.chip.sel { background:var(--accent); color:#0b0e14; border-color:var(--accent); }
  input, .row { font:inherit; }
  input[type=text], input[type=time], input[type=date] {
     background:#0c0f15; border:1px solid var(--line); color:var(--fg); border-radius:8px; padding:8px 10px; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .muted { color:var(--dim); font-size:12px; }
  #plan { flex:1; overflow-y:auto; padding:16px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:12px; }
  .card h3 { margin:0 0 8px; font-size:13px; }
  .kv { color:var(--dim); font-size:12px; } .kv b{ color:var(--fg); }
  pre { background:#0c0f15; border:1px solid var(--line); border-radius:8px; padding:10px; overflow-x:auto;
        font-size:12px; white-space:pre-wrap; }
  .node { border-left:2px solid var(--line); margin:4px 0 4px 8px; padding-left:10px; }
  .warn { color:var(--warn); }
  details summary { cursor:pointer; color:var(--dim); }
  .ab { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width:1100px){ .ab{ grid-template-columns:1fr; } }
  .abcol { min-width:0; }
  .abhd { font-weight:600; color:var(--accent); margin-bottom:6px; font-size:13px; }
  .delta b { color:var(--accent); }
</style></head>
<body>
<header>
  <b>re:action prompt playground</b>
  <span class="tag" id="keytag"></span>
  <span style="flex:1"></span>
  <button id="restart">↻ 새 인터뷰</button>
</header>
<main>
  <section class="col">
    <h2><span>딥 인터뷰</span><span id="remain" class="muted"></span></h2>
    <div id="chat"></div>
    <div id="composer"></div>
  </section>
  <section class="col" style="border-right:none">
    <h2><span>계획 (First Plan)</span></h2>
    <div id="plan"><p class="muted">인터뷰를 어느 정도 진행한 뒤(또는 완료 후) 아래에서 계획을 생성해 보세요.
      thinking 예산을 0 vs 2048 로 바꿔가며 분해 품질·지연을 비교할 수 있어요.</p>
      <div class="card">
        <div class="row">
          <label class="muted">thinking 예산(토큰)</label>
          <input type="number" id="think" value="2048" min="0" step="256" style="width:110px"/>
          <button class="primary" id="genplan">계획 생성</button>
          <button id="abplan">⇄ A/B 비교 (0 vs N)</button>
          <span id="planstat" class="muted"></span>
        </div>
        <div class="muted" style="margin-top:6px">0 = 인터뷰와 동일(끔) · 2048 = 계획만 thinking 켬(프로덕션 기본).
          <b>A/B</b> 는 같은 답으로 thinking 0 과 N 을 각각 생성해 나란히 비교합니다.</div>
      </div>
      <div id="planout"></div>
    </div>
  </section>
</main>
<script>
const HAS_KEY = __HAS_KEY__;
let sid = null, currentSlot = null, sel = [];
const $ = s => document.querySelector(s);
const chat = $('#chat'), composer = $('#composer'), plan = $('#planout');
$('#keytag').innerHTML = HAS_KEY
  ? '<span class="key-ok">● GEMINI_API_KEY 감지 — 실 Gemini</span>'
  : '<span class="key-no">○ 키 없음 — 룰 fallback (문안은 정상, 실측하려면 .env 에 키)</span>';

async function post(path, body){ const r = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})}); return r.json(); }
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function bubble(cls, html){ const d=document.createElement('div'); d.className='bubble '+cls; d.innerHTML=html; chat.appendChild(d); chat.scrollTop=chat.scrollHeight; return d; }
function sys(t){ const d=document.createElement('div'); d.className='sys'; d.textContent=t; chat.appendChild(d); chat.scrollTop=chat.scrollHeight; }

function renderQuestion(q){
  currentSlot = q; sel = [];
  $('#remain').textContent = q.remaining!=null ? ('남은 필수 슬롯 '+q.remaining) : '';
  let e = '<b>'+esc(q.text)+'</b>';
  if(q.empathy) e += '<div class="empathy">'+esc(q.empathy)+'</div>';
  e += '<div class="empathy">['+q.slotKey+' · '+q.answerType+']</div>';
  bubble('ai', e);
  composer.innerHTML='';
  const multi = (q.slotKey==='time.peak_window' || q.slotKey==='time.no_touch');
  if(q.options && q.options.length){
    const wrap=document.createElement('div'); wrap.className='opts';
    q.options.forEach(o=>{ const b=document.createElement('button'); b.className='chip'; b.textContent=o;
      b.onclick=()=>{ if(multi){ b.classList.toggle('sel'); sel=[...composer.querySelectorAll('.chip.sel')].map(x=>x.textContent); }
                      else { send([o]); } };
      wrap.appendChild(b); });
    composer.appendChild(wrap);
    if(multi){ const b=document.createElement('button'); b.className='primary'; b.textContent='선택 보내기';
      b.onclick=()=> sel.length ? send(sel) : null; composer.appendChild(b); }
  } else if(q.answerType==='time_range'){
    const row=document.createElement('div'); row.className='row';
    row.innerHTML='<input type="time" id="ts" value="09:00"/> ~ <input type="time" id="te" value="23:00"/>';
    const b=document.createElement('button'); b.className='primary'; b.textContent='보내기';
    b.onclick=()=> send({start:$('#ts').value, end:$('#te').value}); row.appendChild(b); composer.appendChild(row);
  } else if(q.answerType==='date_picker'){
    const row=document.createElement('div'); row.className='row';
    row.innerHTML='<input type="date" id="dp"/>';
    const b=document.createElement('button'); b.className='primary'; b.textContent='보내기';
    b.onclick=()=> send($('#dp').value||''); const b2=document.createElement('button'); b2.textContent='없음/건너뛰기';
    b2.onclick=()=> send('없음'); row.appendChild(b); row.appendChild(b2); composer.appendChild(row);
  } else {
    const row=document.createElement('div'); row.className='row';
    const i=document.createElement('input'); i.type='text'; i.style.flex='1'; i.placeholder='답을 입력하고 Enter';
    i.onkeydown=e=>{ if(e.key==='Enter'&&i.value.trim()){ send(i.value.trim()); } };
    const b=document.createElement('button'); b.className='primary'; b.textContent='보내기';
    b.onclick=()=> i.value.trim() && send(i.value.trim());
    row.appendChild(i); row.appendChild(b); composer.appendChild(row); i.focus();
    if(q.suggestedAnswers && q.suggestedAnswers.length){
      const w=document.createElement('div'); w.className='opts'; w.style.marginTop='2px';
      const lab=document.createElement('span'); lab.className='muted'; lab.textContent='추천: '; w.appendChild(lab);
      q.suggestedAnswers.forEach(s=>{ const c=document.createElement('button'); c.className='chip'; c.textContent=s;
        c.onclick=()=> send(s); w.appendChild(c); }); composer.appendChild(w);
    }
  }
  // 항상 [충분해요] 종료 버튼
  const fin=document.createElement('button'); fin.textContent='✓ 충분해요 (그만하고 요약)';
  fin.style.marginTop='6px'; fin.onclick=finish; composer.appendChild(fin);
}

function meLabel(v){ return Array.isArray(v)? v.join(', ') : (typeof v==='object'? (v.start+'~'+v.end) : String(v)); }
function showHarvest(r){
  if(r.harvested && r.harvested.length){
    const names = r.harvested.map(h=>h.label).join(', ');
    sys('🪄 이 답변에서 미리 채운 슬롯: '+names+' — 다시 안 물어봐요');
  }
}
async function send(value){
  bubble('me', esc(meLabel(value)));
  composer.innerHTML='<span class="muted">…생각 중</span>';
  const r = await post('/api/answer', {sessionId:sid, value});
  if(r.error){ sys('오류: '+r.error); return; }
  showHarvest(r);
  if(r.done) renderDone(r); else renderQuestion(r.question);
}
async function finish(){
  composer.innerHTML='<span class="muted">…요약 만드는 중</span>';
  const r = await post('/api/finish', {sessionId:sid}); renderDone(r);
}
function renderDone(r){
  composer.innerHTML='';
  $('#remain').textContent='완료 ('+(r.endReason||'')+')';
  const s = r.summary||{};
  let html = '<b>📋 요약 확인 카드</b>';
  if(s.headline) html += '<div style="margin-top:6px"><b>'+esc(s.headline)+'</b></div>';
  ['goal_summary','time_summary','preference_summary'].forEach(k=>{ if(s[k]) html+='<div class="empathy">• '+esc(s[k])+'</div>'; });
  if(s.confirm_question) html += '<div style="margin-top:6px">'+esc(s.confirm_question)+'</div>';
  bubble('ai', html);
  const d=document.createElement('details'); d.innerHTML='<summary>outcome(계획 시드) JSON</summary><pre>'+esc(JSON.stringify(r.outcome,null,2))+'</pre>';
  chat.appendChild(d); chat.scrollTop=chat.scrollHeight;
  sys('이제 오른쪽에서 [계획 생성]으로 이 답변 기반 First Plan 을 만들어 보세요.');
}

function metaBar(r){
  return '<div class="card"><div class="kv">source <b>'+r.aiSource+'</b> · thinking <b>'+r.thinkingBudget+'</b>'
       + ' · <b>'+r.elapsedMs+'ms</b>'+(r.usedFallback?' · <span class="warn">fallback</span>':'')+'</div></div>';
}
function planBodyHtml(r){
  let h = '<div class="card"><h3>분해 (nodes '+r.goalNodes.length+' · actions '+r.actionItems.length+')</h3>';
  r.goalNodes.forEach(n=>{ const acts=r.actionItems.filter(a=>a.node_id===n.node_id);
    h+='<div class="node"><b>'+esc(n.title)+'</b> <span class="kv">('+n.node_type+(n.is_leaf?' · leaf':'')+')</span>';
    acts.forEach(a=> h+='<div class="kv" style="margin-left:8px">↳ '+esc(a.title)+' <b>'+a.estimated_minutes+'분</b> ['+a.category+']'
      + (a.first_step? ' · 첫걸음: '+esc(a.first_step):'')+'</div>');
    h+='</div>'; });
  if(!r.goalNodes.length) h+='<div class="kv">분해 결과 없음</div>';
  h+='</div>';
  if(r.review){ h += '<div class="card"><h3>검토 (approved: '+(r.review.approved?'✅':'❌')+')</h3>';
    (r.review.feedback||[]).forEach(f=> h+='<div class="kv">• '+esc(f)+'</div>');
    if(!r.review.feedback||!r.review.feedback.length) h+='<div class="kv">피드백 없음</div>'; h+='</div>'; }
  if(r.warnings && r.warnings.length){ h+='<div class="card"><h3>배치 경고</h3>';
    r.warnings.forEach(w=> h+='<div class="kv warn">• '+esc(w)+'</div>'); h+='</div>'; }
  if(r.scheduledBlocks && r.scheduledBlocks.length){ h+='<div class="card"><h3>배치 블록 '+r.scheduledBlocks.length+'</h3>';
    r.scheduledBlocks.forEach(b=> h+='<div class="kv">'+esc(b.title)+' · '+b.start+'~'+b.end+'</div>'); h+='</div>'; }
  if(r.policyViolations && r.policyViolations.length){ h+='<div class="card"><h3>정책 위반</h3>';
    r.policyViolations.forEach(p=> h+='<div class="kv warn">• '+esc(p.node_id)+': '+esc(p.reason)+'</div>'); h+='</div>'; }
  return h;
}
function busy(on){ $('#genplan').disabled=on; $('#abplan').disabled=on; $('#planstat').textContent = on?'생성 중…':''; }

async function genPlan(){
  if(!sid){ return; }
  const tb = parseInt($('#think').value||'0',10);
  busy(true);
  const r = await post('/api/plan', {sessionId:sid, thinkingBudget:tb});
  busy(false);
  if(r.error){ plan.innerHTML='<p class="warn">오류: '+esc(r.error)+'</p>'; return; }
  plan.innerHTML = metaBar(r) + planBodyHtml(r);
}

async function abPlan(){
  if(!sid){ return; }
  let nb = parseInt($('#think').value||'0',10); if(nb<=0) nb=2048;
  busy(true);
  plan.innerHTML='<p class="muted">thinking 0 과 '+nb+' 로 각각 생성 중… (실 Gemini 2회 호출, 잠시)</p>';
  const a = await post('/api/plan', {sessionId:sid, thinkingBudget:0});
  const b = await post('/api/plan', {sessionId:sid, thinkingBudget:nb});
  busy(false);
  if(a.error||b.error){ plan.innerHTML='<p class="warn">A/B 오류</p>'; return; }
  const delta = '<div class="card delta"><h3>Δ 델타 (thinking 0 → '+nb+')</h3>'
    + '<div class="kv">goal_nodes <b>'+a.goalNodes.length+' → '+b.goalNodes.length+'</b>'
    + ' · action_items <b>'+a.actionItems.length+' → '+b.actionItems.length+'</b>'
    + ' · 지연 <b>'+a.elapsedMs+'ms → '+b.elapsedMs+'ms</b>'
    + ' · 출력토큰 대리(nodes+actions) 증가폭이 클수록 thinking 효과</div></div>';
  plan.innerHTML = delta + '<div class="ab">'
    + '<div class="abcol"><div class="abhd">A · OFF (thinking 0)</div>'+metaBar(a)+planBodyHtml(a)+'</div>'
    + '<div class="abcol"><div class="abhd">B · ON (thinking '+nb+')</div>'+metaBar(b)+planBodyHtml(b)+'</div>'
    + '</div>';
}

async function start(){
  chat.innerHTML=''; plan.innerHTML='';
  const r = await post('/api/start', {}); sid = r.sessionId; renderQuestion(r.question);
}
$('#restart').onclick = start;
$('#genplan').onclick = genPlan;
$('#abplan').onclick = abPlan;
start();
</script>
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(prog="prompt_playground")
    parser.add_argument("--port", type=int, default=5173, help="포트 (기본 5173)")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import uvicorn

    print(f"▶ re:action prompt playground → http://{args.host}:{args.port}")
    print("  (.env 의 GEMINI_API_KEY 가 있으면 실 Gemini, 없으면 룰 fallback)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
