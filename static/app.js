let analyzing = false;
let editAnalyzing = false;
let planningAnalyzing = false;
let introAnalyzing = false;
let scriptAnalyzing = false;

const ALL_TABS = ['research', 'planning', 'intro', 'script', 'edit', 'history'];

function switchTab(tab) {
  ALL_TABS.forEach(t => {
    document.getElementById(`tab-${t}`).classList.toggle('active', t === tab);
    document.getElementById(`pane-${t}`).classList.toggle('hidden', t !== tab);
  });
  if (tab === 'history') loadHistory('');
}

// ===== 공통 유틸 =====

function renderList(id, items) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '';
  (items || []).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item;
    el.appendChild(li);
  });
}

function fmt(n) {
  if (n >= 100000000) return Math.floor(n / 100000000) + '억';
  if (n >= 10000) return Math.floor(n / 10000) + '만';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return n.toLocaleString();
}

function makeProgressStepper(stepsElId) {
  const el = document.getElementById(stepsElId);
  const icons = { active: '⏳', done: '✅', error: '❌' };
  return function addStep(message, status = 'active') {
    const prev = el.querySelector('.progress-step.active');
    if (prev && status === 'active') {
      prev.className = 'progress-step done';
      prev.querySelector('.step-icon').textContent = icons.done;
    }
    const step = document.createElement('div');
    step.className = `progress-step ${status}`;
    step.innerHTML = `<span class="step-icon">${icons[status]}</span><span>${message}</span>`;
    el.appendChild(step);
    return step;
  };
}

async function streamSSE(url, body, addStep, onDone, onError) {
  let buffer = '';
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`서버 오류: ${resp.status}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.step === 'error') { onError(data.message); return; }
          if (data.step === 'done') { onDone(data); return; }
          if (data.message) addStep(data.message, 'active');
        } catch (e) {}
      }
    }
  } catch (err) {
    onError(err.message);
  }
}

// ===== ① 시장조사 =====

function setKeyword(kw) {
  document.getElementById('keyword-input').value = kw;
  document.getElementById('keyword-input').focus();
}

document.getElementById('keyword-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') startAnalysis();
});

function startAnalysis() {
  if (analyzing) return;
  const keyword = document.getElementById('keyword-input').value.trim();
  if (!keyword) { document.getElementById('keyword-input').focus(); return; }
  runAnalysis(keyword);
}

function resetToSearch() {
  document.getElementById('report-section').classList.add('hidden');
  document.getElementById('progress-section').classList.add('hidden');
  document.getElementById('search-section').classList.remove('hidden');
  document.getElementById('analyze-btn').disabled = false;
  analyzing = false;
}

async function runAnalysis(keyword) {
  analyzing = true;
  document.getElementById('analyze-btn').disabled = true;
  document.getElementById('search-section').classList.add('hidden');
  document.getElementById('report-section').classList.add('hidden');
  document.getElementById('progress-steps').innerHTML = '';
  document.getElementById('progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/analyze', { keyword },
    addStep,
    (data) => {
      document.getElementById('progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('분석 완료!', 'done');
      setTimeout(() => {
        document.getElementById('progress-section').classList.add('hidden');
        renderReport(data.report, keyword);
        document.getElementById('report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        analyzing = false;
        document.getElementById('analyze-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('progress-steps').innerHTML = '';
      makeProgressStepper('progress-steps')(msg, 'error');
      analyzing = false;
      document.getElementById('analyze-btn').disabled = false;
    }
  );
}

let _currentReport = null;
let _currentKeyword = '';

function renderReport(r, keyword) {
  _currentReport = r;
  _currentKeyword = keyword;

  document.getElementById('report-keyword-title').textContent = `"${keyword}" 시장조사 결과`;
  document.getElementById('report-subtitle').textContent = `유튜브 상위 영상 댓글 + 네이버 카페 데이터 기반`;

  const cb = document.getElementById('concept-banner');
  cb.textContent = r.one_line_concept || '';
  cb.style.display = r.one_line_concept ? '' : 'none';

  document.getElementById('summary-text').textContent = r.summary || '';

  renderList('desire-curiosity', r.desire_analysis?.curiosity);
  renderList('desire-complaints', r.desire_analysis?.complaints);
  renderList('desire-wants', r.desire_analysis?.wants);

  const ql = document.getElementById('top-questions');
  ql.innerHTML = '';
  (r.top_questions || []).forEach((q, i) => {
    const li = document.createElement('li');
    li.innerHTML = `<span class="q-num">${i + 1}</span><span>${q}</span>`;
    ql.appendChild(li);
  });

  renderList('must-include', r.must_include_content);

  const dg = document.getElementById('diff-points');
  dg.innerHTML = '';
  (r.differentiation_points || []).forEach(p => {
    const div = document.createElement('div');
    div.className = 'diff-card';
    div.textContent = p;
    dg.appendChild(div);
  });

  renderList('title-patterns', r.competitor_analysis?.title_patterns);
  renderList('thumb-styles', r.competitor_analysis?.thumbnail_styles);
  const kc = document.getElementById('keywords-cloud');
  kc.innerHTML = '';
  (r.competitor_analysis?.popular_keywords || []).forEach(kw => {
    const span = document.createElement('span');
    span.className = 'kw-tag';
    span.textContent = kw;
    kc.appendChild(span);
  });

  const vl = document.getElementById('top-videos-list');
  vl.innerHTML = '';
  (r.top_videos || []).forEach(v => {
    const card = document.createElement('div');
    card.className = 'video-thumb-card';
    card.innerHTML = `
      <a href="${v.url}" target="_blank" rel="noopener">
        <img src="${v.thumbnail}" alt="${v.title}" loading="lazy" onerror="this.style.background='#ddd'" />
        <div class="video-thumb-info">
          <div class="video-thumb-title">${v.title}</div>
          <div class="video-thumb-views">조회수 ${fmt(v.views)}회 · ${v.channel}</div>
          ${v.success_reason ? `<div class="video-thumb-reason">${v.success_reason}</div>` : ''}
        </div>
      </a>
    `;
    vl.appendChild(card);
  });

  const ss = document.getElementById('video-structure');
  ss.innerHTML = '';
  (r.recommended_structure || []).forEach((step, i, arr) => {
    const div = document.createElement('div');
    div.className = 'structure-step';
    const isLast = i === arr.length - 1;
    div.innerHTML = `
      <div class="step-line-wrap">
        <div class="step-circle">${i + 1}</div>
        ${!isLast ? '<div class="step-connector"></div>' : ''}
      </div>
      <div class="step-content"><div class="step-text">${step}</div></div>
    `;
    ss.appendChild(div);
  });
}

function downloadPDF() {
  if (!_currentReport) return;
  openPrintWindow(_currentReport, _currentKeyword, 'research');
}

function openPrintWindow(report, keyword, type) {
  const saved = { r: _currentReport, k: _currentKeyword };
  _currentReport = report; _currentKeyword = keyword;
  let reportEl, html;
  if (type === 'research') {
    reportEl = document.getElementById('report-section');
    renderReport(report, keyword);
    html = reportEl.innerHTML;
  } else {
    reportEl = document.getElementById('edit-report-section');
    renderEditReport(report, keyword);
    html = reportEl.innerHTML;
  }
  _currentReport = saved.r; _currentKeyword = saved.k;
  _printHTML(keyword, html, 'report-section');
}

function _printHTML(title, bodyHTML, wrapClass) {
  const style = Array.from(document.styleSheets)
    .map(s => { try { return Array.from(s.cssRules).map(r => r.cssText).join('\n'); } catch { return ''; } })
    .join('\n');
  const html = `<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"/><title>${title}</title>
<style>${style}
body{background:#fff!important;margin:0;padding:24px;font-family:'Apple SD Gothic Neo','Noto Sans KR',sans-serif;}
.report-header-actions,.reset-btn{display:none!important;}
.card{box-shadow:none!important;border:1px solid #e5e7eb;break-inside:avoid;}
.videos-scroll{overflow:visible;flex-wrap:wrap;}
@media print{body{padding:0;}.card{break-inside:avoid;}}
</style></head><body>
<h2 style="font-size:22px;font-weight:800;margin-bottom:4px">"${title}"</h2>
<p style="color:#6b7280;font-size:13px;margin-bottom:20px">유튜브 콘텐츠 리서처 · AI 분석 결과</p>
<div class="${wrapClass}" style="padding:0;max-width:100%">${bodyHTML}</div>
<script>window.onload=function(){document.querySelectorAll('button').forEach(b=>b.style.display='none');setTimeout(()=>window.print(),400);};<\/script>
</body></html>`;
  const win = window.open('', '_blank');
  if (!win) { alert('팝업이 차단되었습니다.'); return; }
  win.document.write(html);
  win.document.close();
}

function copyReportText() {
  if (!_currentReport) return;
  const r = _currentReport;
  let text = `📊 "${_currentKeyword}" 시장조사 결과\n${'='.repeat(40)}\n\n`;
  if (r.one_line_concept) text += `💎 핵심 컨셉\n${r.one_line_concept}\n\n`;
  if (r.summary) text += `📋 시장 요약\n${r.summary}\n\n`;
  if (r.desire_analysis) {
    text += `💡 시청자 욕구\n`;
    (r.desire_analysis.curiosity||[]).forEach(i => text += `  🤔 ${i}\n`);
    (r.desire_analysis.complaints||[]).forEach(i => text += `  😤 ${i}\n`);
    (r.desire_analysis.wants||[]).forEach(i => text += `  ✨ ${i}\n`);
    text += '\n';
  }
  if (r.top_questions?.length) {
    text += `❓ 시청자 TOP 질문\n`;
    r.top_questions.forEach((q, i) => text += `  ${i+1}. ${q}\n`);
    text += '\n';
  }
  if (r.must_include_content?.length) {
    text += `📝 반드시 넣어야 할 내용\n`;
    r.must_include_content.forEach(i => text += `  • ${i}\n`);
    text += '\n';
  }
  if (r.differentiation_points?.length) {
    text += `⚡ 차별화 포인트\n`;
    r.differentiation_points.forEach(i => text += `  • ${i}\n`);
  }
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('#report-section .copy-btn');
    if (btn) { btn.textContent = '✅ 복사됨!'; setTimeout(() => btn.textContent = '📋 텍스트 복사', 2000); }
  }).catch(() => alert('복사 실패'));
}

// ===== ② 기획 (문제 정의 + 제목 + 썸네일) =====

function resetToPlanning() {
  document.getElementById('planning-report-section').classList.add('hidden');
  document.getElementById('planning-progress-section').classList.add('hidden');
  document.getElementById('planning-input-section').classList.remove('hidden');
  document.getElementById('planning-btn').disabled = false;
  planningAnalyzing = false;
}

function startPlanning() {
  if (planningAnalyzing) return;
  const keyword = document.getElementById('planning-keyword-input').value.trim();
  const product_desc = document.getElementById('planning-product-input').value.trim();
  if (!keyword) { document.getElementById('planning-keyword-input').focus(); return; }
  if (!product_desc) { document.getElementById('planning-product-input').focus(); return; }
  const market_insights = document.getElementById('planning-insights-input').value.trim();
  runPlanning(keyword, product_desc, market_insights);
}

async function runPlanning(keyword, product_desc, market_insights) {
  planningAnalyzing = true;
  document.getElementById('planning-btn').disabled = true;
  document.getElementById('planning-input-section').classList.add('hidden');
  document.getElementById('planning-report-section').classList.add('hidden');
  document.getElementById('planning-progress-steps').innerHTML = '';
  document.getElementById('planning-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('planning-progress-steps');
  addStep('문제 정의 + 제목 + 썸네일 기획 중...', 'active');

  await streamSSE(
    '/api/planning', { keyword, product_desc, market_insights },
    addStep,
    (data) => {
      document.getElementById('planning-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('완성!', 'done');
      setTimeout(() => {
        document.getElementById('planning-progress-section').classList.add('hidden');
        renderPlanningReport(data.report, keyword);
        document.getElementById('planning-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        planningAnalyzing = false;
        document.getElementById('planning-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('planning-progress-steps').innerHTML = '';
      makeProgressStepper('planning-progress-steps')(msg, 'error');
      planningAnalyzing = false;
      document.getElementById('planning-btn').disabled = false;
    }
  );
}

function renderPlanningReport(r, keyword) {
  document.getElementById('planning-report-title').textContent = `"${keyword}" 기획안`;

  // 문제 정의
  const pd = r.problem_definition || {};
  const problemEl = document.getElementById('planning-problem');
  problemEl.innerHTML = `
    <div class="problem-def-item">
      <div class="problem-def-label">📍 현상 (지금 시청자의 상황)</div>
      <div class="problem-def-text">${pd.current_situation || ''}</div>
    </div>
    <div class="problem-def-item">
      <div class="problem-def-label">✨ 욕구 (시청자가 원하는 결과)</div>
      <div class="problem-def-text">${pd.desired_outcome || ''}</div>
    </div>
    <div class="problem-def-item" style="grid-column:1/-1">
      <div class="problem-def-label">🎯 핵심 문제</div>
      <div class="problem-def-text" style="font-size:16px;font-weight:700;color:#1e293b">${pd.core_problem || ''}</div>
    </div>
    <div class="problem-def-item" style="grid-column:1/-1">
      <div class="problem-def-label">💡 이 영상의 해결 각도</div>
      <div class="problem-def-text">${pd.solution_angle || ''}</div>
    </div>
  `;

  // 제목 후보
  const tg = document.getElementById('planning-titles');
  tg.innerHTML = '';
  (r.recommended_titles || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'title-card';
    div.innerHTML = `
      <div class="title-num">제목 ${i + 1}</div>
      <div class="title-text">${t.title || t}</div>
      ${t.ctr_strategy ? `<div class="title-hook" style="color:#f59e0b">전략: ${t.ctr_strategy}</div>` : ''}
      ${t.hook_reason ? `<div class="title-hook">${t.hook_reason}</div>` : ''}
      ${t.strength ? `<span class="title-emotion">${t.strength}</span>` : ''}
    `;
    tg.appendChild(div);
  });

  // 썸네일 컨셉
  const thg = document.getElementById('planning-thumbnails');
  thg.innerHTML = '';
  (r.thumbnail_concepts || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'thumb-concept-card';
    div.innerHTML = `
      <div class="thumb-concept-num">썸네일 ${i + 1} — ${t.concept_name || ''}</div>
      <div class="thumb-main-text">"${t.main_text || ''}"</div>
      ${t.sub_text ? `<div class="thumb-sub-text">${t.sub_text}</div>` : ''}
      <div class="thumb-concept-detail"><strong>🎨 색상/분위기:</strong> ${t.color_mood || ''}</div>
      <div class="thumb-concept-detail"><strong>📸 이미지/구도:</strong> ${t.visual || ''}</div>
      ${t.expression ? `<div class="thumb-concept-detail"><strong>😊 표정/포즈:</strong> ${t.expression}</div>` : ''}
      <div class="thumb-concept-why">${t.why_clicks || ''}</div>
    `;
    thg.appendChild(div);
  });
}

// ===== ③ 도입부 작성 =====

let _currentIntroScript = '';

function resetToIntro() {
  document.getElementById('intro-report-section').classList.add('hidden');
  document.getElementById('intro-progress-section').classList.add('hidden');
  document.getElementById('intro-input-section').classList.remove('hidden');
  document.getElementById('intro-btn').disabled = false;
  introAnalyzing = false;
}

function startIntro() {
  if (introAnalyzing) return;
  const keyword = document.getElementById('intro-keyword-input').value.trim();
  const product_desc = document.getElementById('intro-product-input').value.trim();
  const problem_definition = document.getElementById('intro-problem-input').value.trim();
  const viewer_desire = document.getElementById('intro-desire-input').value.trim();
  if (!keyword) { document.getElementById('intro-keyword-input').focus(); return; }
  if (!problem_definition) { document.getElementById('intro-problem-input').focus(); return; }
  if (!viewer_desire) { document.getElementById('intro-desire-input').focus(); return; }
  runIntro(keyword, product_desc, problem_definition, viewer_desire);
}

async function runIntro(keyword, product_desc, problem_definition, viewer_desire) {
  introAnalyzing = true;
  document.getElementById('intro-btn').disabled = true;
  document.getElementById('intro-input-section').classList.add('hidden');
  document.getElementById('intro-report-section').classList.add('hidden');
  document.getElementById('intro-progress-steps').innerHTML = '';
  document.getElementById('intro-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('intro-progress-steps');
  addStep('도입부 대본 작성 중...', 'active');

  await streamSSE(
    '/api/intro', { keyword, product_desc, problem_definition, viewer_desire },
    addStep,
    (data) => {
      document.getElementById('intro-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('완성!', 'done');
      setTimeout(() => {
        document.getElementById('intro-progress-section').classList.add('hidden');
        renderIntroReport(data.report, keyword);
        document.getElementById('intro-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        introAnalyzing = false;
        document.getElementById('intro-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('intro-progress-steps').innerHTML = '';
      makeProgressStepper('intro-progress-steps')(msg, 'error');
      introAnalyzing = false;
      document.getElementById('intro-btn').disabled = false;
    }
  );
}

const STAGE_COLORS = {
  '문제제기': '#ef4444',
  '공감': '#f59e0b',
  '손해': '#8b5cf6',
  '이득': '#10b981',
  '사례': '#3b82f6',
};

function renderIntroReport(r, keyword) {
  _currentIntroScript = r.full_intro || '';
  document.getElementById('intro-report-title').textContent = `"${keyword}" 도입부 대본`;
  document.getElementById('intro-full-script').textContent = r.full_intro || '';

  // 단계별 분석
  const breakdownEl = document.getElementById('intro-breakdown');
  breakdownEl.innerHTML = '';
  (r.breakdown || []).forEach((item) => {
    const color = STAGE_COLORS[item.stage] || '#6b7280';
    const div = document.createElement('div');
    div.className = 'intro-breakdown-item';
    div.innerHTML = `
      <div class="intro-stage-badge" style="background:${color}20;color:${color};border:1px solid ${color}40">
        ${item.stage} · ${item.duration_sec || ''}초
      </div>
      <div class="intro-stage-script">"${item.text || ''}"</div>
      <div class="intro-stage-purpose">${item.purpose || ''}</div>
    `;
    breakdownEl.appendChild(div);
  });

  renderList('intro-hook-variations', r.hook_variations);
  renderList('intro-filming-tips', r.filming_tips);
}

function copyIntroScript() {
  if (!_currentIntroScript) return;
  navigator.clipboard.writeText(_currentIntroScript).then(() => {
    const btn = document.querySelector('#intro-report-section .copy-btn');
    if (btn) { btn.textContent = '✅ 복사됨!'; setTimeout(() => btn.textContent = '📋 대본 복사', 2000); }
  }).catch(() => alert('복사 실패'));
}

// ===== ④ 대본 작성 =====

let _currentAdaptedScript = '';

function resetToScript() {
  document.getElementById('script-report-section').classList.add('hidden');
  document.getElementById('script-progress-section').classList.add('hidden');
  document.getElementById('script-input-section').classList.remove('hidden');
  document.getElementById('script-btn').disabled = false;
  scriptAnalyzing = false;
}

function startScript() {
  if (scriptAnalyzing) return;
  const keyword = document.getElementById('script-keyword-input').value.trim();
  const product_desc = document.getElementById('script-product-input').value.trim();
  const reference_script = document.getElementById('script-ref-input').value.trim();
  const context = document.getElementById('script-context-input').value.trim();
  if (!keyword) { document.getElementById('script-keyword-input').focus(); return; }
  if (!reference_script) { document.getElementById('script-ref-input').focus(); return; }
  runScript(keyword, product_desc, reference_script, context);
}

async function runScript(keyword, product_desc, reference_script, context) {
  scriptAnalyzing = true;
  document.getElementById('script-btn').disabled = true;
  document.getElementById('script-input-section').classList.add('hidden');
  document.getElementById('script-report-section').classList.add('hidden');
  document.getElementById('script-progress-steps').innerHTML = '';
  document.getElementById('script-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('script-progress-steps');
  addStep('레퍼런스 분석 + 대본 변형 중...', 'active');

  await streamSSE(
    '/api/script', { keyword, product_desc, reference_script, context },
    addStep,
    (data) => {
      document.getElementById('script-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('완성!', 'done');
      setTimeout(() => {
        document.getElementById('script-progress-section').classList.add('hidden');
        renderScriptReport(data.report, keyword);
        document.getElementById('script-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        scriptAnalyzing = false;
        document.getElementById('script-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('script-progress-steps').innerHTML = '';
      makeProgressStepper('script-progress-steps')(msg, 'error');
      scriptAnalyzing = false;
      document.getElementById('script-btn').disabled = false;
    }
  );
}

function renderScriptReport(r, keyword) {
  _currentAdaptedScript = r.adapted_script || '';
  document.getElementById('script-report-title').textContent = `"${keyword}" 변형 대본`;

  renderList('script-ref-structure', r.reference_structure_analysis);

  document.getElementById('script-adapted').textContent = r.adapted_script || '';

  // 섹션별
  const sectionsEl = document.getElementById('script-sections');
  sectionsEl.innerHTML = '';
  (r.sections || []).forEach((s, i) => {
    const div = document.createElement('div');
    div.className = 'script-section-item';
    div.innerHTML = `
      <div class="script-section-name">📌 ${s.section_name || ''}</div>
      <div class="script-section-row">
        <div class="script-section-half">
          <div class="script-section-label">레퍼런스 방식</div>
          <div class="script-section-val">${s.original_approach || ''}</div>
        </div>
        <div class="script-section-half">
          <div class="script-section-label">내 버전</div>
          <div class="script-section-val">${s.my_version || ''}</div>
        </div>
      </div>
      ${s.script ? `<div class="script-section-text">"${s.script}"</div>` : ''}
    `;
    sectionsEl.appendChild(div);
  });

  // 추가 요소
  const enhEl = document.getElementById('script-enhancements');
  enhEl.innerHTML = '';
  (r.enhancement_suggestions || []).forEach(e => {
    const div = document.createElement('div');
    div.className = 'enhancement-card';
    div.innerHTML = `
      <div class="enhancement-type">${e.type}</div>
      <div class="enhancement-suggestion">${e.suggestion}</div>
      <div class="enhancement-why">${e.why}</div>
    `;
    enhEl.appendChild(div);
  });

  // 댓글 유도
  const ci = r.comment_inducing || {};
  const commentEl = document.getElementById('script-comment');
  commentEl.innerHTML = `
    <div style="font-weight:700;color:#ef4444;margin-bottom:10px">전략: ${ci.strategy || ''}</div>
    <div class="intro-script-output" style="border-left-color:#ef4444;margin-bottom:16px">${ci.ending_script || ''}</div>
    ${(ci.question_variations || []).map((q, i) => `
      <div class="comment-question-item">
        <span class="comment-q-num">Q${i+1}</span>
        <span>${q}</span>
      </div>
    `).join('')}
  `;
}

function copyAdaptedScript() {
  if (!_currentAdaptedScript) return;
  navigator.clipboard.writeText(_currentAdaptedScript).then(() => {
    const btn = document.querySelector('#script-report-section .copy-btn');
    if (btn) { btn.textContent = '✅ 복사됨!'; setTimeout(() => btn.textContent = '📋 대본 복사', 2000); }
  }).catch(() => alert('복사 실패'));
}

// ===== ⑤ 편집 피드백 =====

document.addEventListener('DOMContentLoaded', () => {
  const ei = document.getElementById('edit-keyword-input');
  if (ei) ei.addEventListener('keydown', e => { if (e.key === 'Enter') startEditAnalysis(); });
});

function resetToEdit() {
  document.getElementById('edit-report-section').classList.add('hidden');
  document.getElementById('edit-progress-section').classList.add('hidden');
  document.getElementById('edit-input-section').classList.remove('hidden');
  document.getElementById('edit-analyze-btn').disabled = false;
  editAnalyzing = false;
}

function startEditAnalysis() {
  if (editAnalyzing) return;
  const keyword = document.getElementById('edit-keyword-input').value.trim();
  const script = document.getElementById('edit-script-input').value.trim();
  if (!keyword) { document.getElementById('edit-keyword-input').focus(); return; }
  if (!script) { document.getElementById('edit-script-input').focus(); return; }
  runEditAnalysis(keyword, script);
}

async function runEditAnalysis(keyword, script) {
  editAnalyzing = true;
  document.getElementById('edit-analyze-btn').disabled = true;
  document.getElementById('edit-input-section').classList.add('hidden');
  document.getElementById('edit-report-section').classList.add('hidden');
  document.getElementById('edit-progress-steps').innerHTML = '';
  document.getElementById('edit-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('edit-progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/edit-feedback', { keyword, script },
    addStep,
    (data) => {
      document.getElementById('edit-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('분석 완료!', 'done');
      setTimeout(() => {
        document.getElementById('edit-progress-section').classList.add('hidden');
        renderEditReport(data.report, keyword);
        document.getElementById('edit-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        editAnalyzing = false;
        document.getElementById('edit-analyze-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('edit-progress-steps').innerHTML = '';
      makeProgressStepper('edit-progress-steps')(msg, 'error');
      editAnalyzing = false;
      document.getElementById('edit-analyze-btn').disabled = false;
    }
  );
}

function renderEditReport(r, keyword) {
  document.getElementById('edit-report-title').textContent = `"${keyword}" 편집 피드백`;

  const score = r.market_fit_score || 0;
  const scoreColor = score >= 70 ? '#10b981' : score >= 40 ? '#f59e0b' : '#ef4444';
  const scoreLabel = score >= 70 ? '시장 적합도 높음' : score >= 40 ? '보완 필요' : '대폭 수정 필요';
  document.getElementById('fit-score-banner').innerHTML = `
    <div class="fit-score-inner">
      <div class="fit-score-num" style="color:${scoreColor}">${score}</div>
      <div class="fit-score-info">
        <div class="fit-score-label" style="color:${scoreColor}">${scoreLabel}</div>
        <div class="fit-score-sub">시장 데이터 기반 시청자 관심도 예측</div>
      </div>
    </div>
  `;

  document.getElementById('edit-overall').textContent = r.overall_assessment || '';
  renderList('edit-strengths', r.strengths);

  const keepEl = document.getElementById('edit-keep-sections');
  keepEl.innerHTML = '';
  (r.keep_sections || []).forEach(s => {
    const div = document.createElement('div');
    div.className = 'edit-section-item keep-item';
    const badge = s.priority === 'high' ? '<span class="priority-badge high">필수</span>' : '<span class="priority-badge medium">권장</span>';
    div.innerHTML = `<div class="edit-section-title">${badge} ${s.section}</div><div class="edit-section-reason">${s.reason}</div>`;
    keepEl.appendChild(div);
  });

  const cutEl = document.getElementById('edit-cut-sections');
  cutEl.innerHTML = '';
  (r.cut_sections || []).forEach(s => {
    const div = document.createElement('div');
    div.className = 'edit-section-item cut-item';
    div.innerHTML = `
      <div class="edit-section-title">${s.section}</div>
      <div class="edit-section-reason">${s.reason}</div>
      ${s.alternative ? `<div class="edit-section-alt">대안: ${s.alternative}</div>` : ''}
    `;
    cutEl.appendChild(div);
  });

  document.getElementById('edit-hook-feedback').textContent = r.hook_feedback || '';
  renderList('edit-flow-suggestions', r.edit_flow_suggestions);
  renderList('edit-missing', r.missing_content);

  const tg = document.getElementById('edit-titles');
  tg.innerHTML = '';
  (r.recommended_titles || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'title-card';
    div.innerHTML = `
      <div class="title-num">제목 ${i + 1}</div>
      <div class="title-text">${t.title || t}</div>
      ${t.hook_reason ? `<div class="title-hook">${t.hook_reason}</div>` : ''}
      ${t.target_emotion ? `<span class="title-emotion">${t.target_emotion}</span>` : ''}
    `;
    tg.appendChild(div);
  });

  const th = document.getElementById('edit-thumbnails');
  th.innerHTML = '';
  (r.thumbnail_recommendations || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'thumb-reco-card';
    div.innerHTML = `
      <div class="thumb-reco-num">썸네일 ${i + 1}</div>
      <div class="thumb-reco-main-text">"${t.main_text}"</div>
      <div class="thumb-reco-concept">${t.concept}</div>
      <div class="thumb-reco-visual">🖼 ${t.visual_element}</div>
      <div class="thumb-reco-reason">${t.reason}</div>
    `;
    th.appendChild(div);
  });
}

// ===== 히스토리 =====

async function downloadHistoryPDF(id) {
  const data = await fetch(`/api/history/${id}`).then(r => r.json());
  if (['research', 'edit'].includes(data.type)) {
    openPrintWindow(data.report, data.keyword, data.type);
  }
}

async function loadHistory(type) {
  ['all','research','planning','intro','script','edit'].forEach(t => {
    const el = document.getElementById(`hf-${t}`);
    if (el) el.classList.toggle('active', (type || '') === (t === 'all' ? '' : t));
  });

  const url = type ? `/api/history?type=${type}` : '/api/history';
  const data = await fetch(url).then(r => r.json());
  const list = document.getElementById('history-list');
  document.getElementById('history-count').textContent = `총 ${data.length}건`;

  if (!data.length) {
    list.innerHTML = '<div class="history-empty">저장된 결과가 없습니다.<br>각 탭에서 분석을 실행하면 자동으로 저장됩니다.</div>';
    return;
  }

  const typeLabels = { research: '시장조사', edit: '편집 피드백', planning: '기획', intro: '도입부', script: '대본' };
  const typeColors = { research: '#3b82f6', edit: '#8b5cf6', planning: '#f59e0b', intro: '#10b981', script: '#ef4444' };

  list.innerHTML = '';
  data.forEach(item => {
    const typeLabel = typeLabels[item.type] || item.type;
    const typeColor = typeColors[item.type] || '#6b7280';
    const date = item.created_at?.slice(0, 16).replace('T', ' ') || '';
    const card = document.createElement('div');
    card.className = 'history-card';
    card.innerHTML = `
      <div class="history-card-inner" onclick="loadHistoryItem(${item.id})">
        <div class="history-card-top">
          <span class="history-type-badge" style="background:${typeColor}20;color:${typeColor}">${typeLabel}</span>
          <span class="history-date">${date}</span>
        </div>
        <div class="history-keyword">"${item.keyword}"</div>
      </div>
      <div class="history-card-actions">
        <button class="history-del-btn" onclick="deleteHistoryItem(${item.id}, this)" title="삭제">✕</button>
      </div>
    `;
    list.appendChild(card);
  });
}

async function loadHistoryItem(id) {
  const data = await fetch(`/api/history/${id}`).then(r => r.json());

  const actions = {
    research: () => {
      switchTab('research'); resetToSearch();
      setTimeout(() => {
        renderReport(data.report, data.keyword);
        document.getElementById('report-section').classList.remove('hidden');
        document.getElementById('search-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    planning: () => {
      switchTab('planning'); resetToPlanning();
      setTimeout(() => {
        renderPlanningReport(data.report, data.keyword);
        document.getElementById('planning-report-section').classList.remove('hidden');
        document.getElementById('planning-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    intro: () => {
      switchTab('intro'); resetToIntro();
      setTimeout(() => {
        renderIntroReport(data.report, data.keyword);
        document.getElementById('intro-report-section').classList.remove('hidden');
        document.getElementById('intro-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    script: () => {
      switchTab('script'); resetToScript();
      setTimeout(() => {
        renderScriptReport(data.report, data.keyword);
        document.getElementById('script-report-section').classList.remove('hidden');
        document.getElementById('script-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    edit: () => {
      switchTab('edit'); resetToEdit();
      setTimeout(() => {
        renderEditReport(data.report, data.keyword);
        document.getElementById('edit-report-section').classList.remove('hidden');
        document.getElementById('edit-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
  };

  (actions[data.type] || actions.research)();
}

async function deleteHistoryItem(id, btn) {
  if (!confirm('이 항목을 삭제하시겠습니까?')) return;
  btn.disabled = true;
  await fetch(`/api/history/${id}`, { method: 'DELETE' });
  btn.closest('.history-card').remove();
  const remaining = document.getElementById('history-list').querySelectorAll('.history-card').length;
  document.getElementById('history-count').textContent = `총 ${remaining}건`;
  if (!remaining) document.getElementById('history-list').innerHTML = '<div class="history-empty">저장된 결과가 없습니다.</div>';
}
