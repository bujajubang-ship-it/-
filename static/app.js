let midformAnalyzing = false;
let shortformAnalyzing = false;
let editAnalyzing = false;
// kept for history backwards compatibility
let analyzing = false;
let planningAnalyzing = false;
let introAnalyzing = false;
let scriptAnalyzing = false;

const ALL_TABS = ['midform', 'shortform', 'topic', 'edit', 'decision', 'channel', 'chat', 'history', 'research', 'planning', 'intro', 'script'];

function switchTab(tab) {
  ALL_TABS.forEach(t => {
    const btn = document.getElementById(`tab-${t}`);
    if (btn) btn.classList.toggle('active', t === tab);
    const pane = document.getElementById(`pane-${t}`);
    if (pane) pane.classList.toggle('hidden', t !== tab);
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
      if (done) { onError('서버 연결이 끊어졌습니다. 다시 시도해주세요.'); return; }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.step === 'error') { onError(data.message); return; }
          if (data.step === 'done') { onDone(data); return; }
          if (data.step === 'ping') continue;
          if (data.message) addStep(data.message, 'active');
        } catch (e) {}
      }
    }
  } catch (err) {
    onError(err.message);
  }
}

// ===== 🔍 주제 추천 =====

let topicAnalyzing = false;

function downloadPdf(sectionId, baseName) {
  const el = document.getElementById(sectionId);
  const date = new Date().toLocaleDateString('ko-KR').replace(/\. ?/g, '-').replace(/-$/, '');
  const filename = `${baseName}_${date}.pdf`;
  const btn = el.querySelector('.pdf-btn');
  if (btn) { btn.textContent = '⏳ 생성 중...'; btn.disabled = true; }
  html2pdf().set({
    margin: [10, 10, 10, 10],
    filename,
    image: { type: 'jpeg', quality: 0.95 },
    html2canvas: { scale: 2, useCORS: true, scrollY: 0 },
    jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
    pagebreak: { mode: ['avoid-all', 'css'] }
  }).from(el).save().then(() => {
    if (btn) { btn.textContent = '⬇ PDF 저장'; btn.disabled = false; }
  });
}

function downloadTopicPdf() {
  downloadPdf('topic-report-section', '부자주방_주제추천');
}

function resetToTopic() {
  document.getElementById('topic-report-section').classList.add('hidden');
  document.getElementById('topic-progress-section').classList.add('hidden');
  document.getElementById('topic-input-section').classList.remove('hidden');
  document.getElementById('topic-btn').disabled = false;
  topicAnalyzing = false;
}

function startTopicSuggest() {
  if (topicAnalyzing) return;
  topicAnalyzing = true;
  document.getElementById('topic-btn').disabled = true;
  document.getElementById('topic-input-section').classList.add('hidden');
  document.getElementById('topic-report-section').classList.add('hidden');
  document.getElementById('topic-progress-steps').innerHTML = '';
  document.getElementById('topic-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('topic-progress-steps');
  addStep('분석 준비 중...', 'active');

  streamSSE(
    '/api/topic-suggest', {},
    addStep,
    (data) => {
      document.getElementById('topic-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('분석 완료!', 'done');
      setTimeout(() => {
        document.getElementById('topic-progress-section').classList.add('hidden');
        renderTopicReport(data.report);
        document.getElementById('topic-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        topicAnalyzing = false;
        document.getElementById('topic-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('topic-progress-steps').innerHTML = '';
      makeProgressStepper('topic-progress-steps')(msg, 'error');
      topicAnalyzing = false;
      document.getElementById('topic-btn').disabled = false;
    }
  );
}

const URGENCY_LABEL = { high: '🔥 지금 당장', medium: '⚡ 이번 주 안에', low: '📌 여유 있을 때' };
const URGENCY_COLOR = { high: '#ef4444', medium: '#f59e0b', low: '#6b7280' };
const CTYPE_CONFIG = {
  '풀링+키': { icon: '⭐', bg: 'linear-gradient(135deg,#fef3c7,#d1fae5)', color: '#065f46', border: '#6ee7b7', label: '풀링+키 겸용' },
  '풀링': { icon: '🧲', bg: '#eff6ff', color: '#1e40af', border: '#93c5fd', label: '풀링 컨텐츠' },
  '키': { icon: '💰', bg: '#fff7ed', color: '#9a3412', border: '#fed7aa', label: '키 컨텐츠' },
};

function renderTopicReport(r) {
  const now = new Date().toLocaleDateString('ko-KR', { month: 'long', day: 'numeric' });
  document.getElementById('topic-report-subtitle').textContent = `${now} 기준 트렌드 분석`;

  const ts = document.getElementById('topic-trend-summary');
  ts.textContent = r.trend_summary || '';
  ts.style.display = r.trend_summary ? '' : 'none';

  // 콘텐츠 믹스 노트
  if (r.content_mix_note) {
    const note = document.createElement('div');
    note.className = 'topic-mix-note';
    note.innerHTML = `<strong>📋 발행 전략:</strong> ${r.content_mix_note}`;
    const reportSection = document.getElementById('topic-report-section');
    const existing = reportSection.querySelector('.topic-mix-note');
    if (existing) existing.remove();
    document.getElementById('topic-hot-topics').closest('.card').insertAdjacentElement('beforebegin', note);
  }

  // 추천 주제 카드
  const container = document.getElementById('topic-hot-topics');
  container.innerHTML = '';
  (r.hot_topics || []).forEach((t, i) => {
    const urgency = t.urgency || 'medium';
    const uColor = URGENCY_COLOR[urgency] || '#6b7280';
    const uLabel = URGENCY_LABEL[urgency] || urgency;
    const ct = t.content_type || '풀링';
    const ctKey = ct.includes('+') ? '풀링+키' : ct.includes('키') ? '키' : '풀링';
    const ctCfg = CTYPE_CONFIG[ctKey] || CTYPE_CONFIG['풀링'];

    const div = document.createElement('div');
    div.className = 'topic-card';
    div.innerHTML = `
      <div class="topic-card-top">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span class="topic-content-type-badge" style="background:${ctCfg.bg};color:${ctCfg.color};border:1.5px solid ${ctCfg.border}">${ctCfg.icon} ${ctCfg.label}</span>
          <span class="topic-urgency-badge" style="background:${uColor}20;color:${uColor};border:1px solid ${uColor}40">${uLabel}</span>
        </div>
        <span class="topic-num">추천 ${i + 1}</span>
      </div>
      <div class="topic-title">${t.title || ''}</div>
      ${t.content_type_reason ? `<div class="topic-type-reason">${ctCfg.icon} ${t.content_type_reason}</div>` : ''}
      ${t.sell_angle ? `<div class="topic-sell-angle">💰 판매 연결: <strong>${t.sell_angle}</strong></div>` : ''}
      <div class="topic-why-now">📊 ${t.why_now || ''}</div>
      <div class="topic-details-grid">
        <div class="topic-detail-item">
          <div class="topic-detail-label">시청자 고민</div>
          <div class="topic-detail-val">${t.viewer_pain || ''}</div>
        </div>
        <div class="topic-detail-item">
          <div class="topic-detail-label">경쟁 영상 빈틈</div>
          <div class="topic-detail-val">${t.content_gap || ''}</div>
        </div>
      </div>
      ${t.urgency_reason ? `<div class="topic-urgency-reason">⏰ ${t.urgency_reason}</div>` : ''}
      <button class="topic-start-btn" onclick="startMidformFromTopic('${(t.keyword || t.title || '').replace(/'/g, "\\'")}')">이 주제로 기획 시작 →</button>
    `;
    container.appendChild(div);
  });

  document.getElementById('topic-cafe-insights').textContent = r.cafe_insights || '';
  document.getElementById('topic-competitor-insights').textContent = r.competitor_insights || '';
  renderList('topic-avoid', r.avoid_topics);
}

function startMidformFromTopic(keyword) {
  document.getElementById('midform-keyword-input').value = keyword;
  switchTab('midform');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ===== 🎬 미드폼 =====

document.addEventListener('DOMContentLoaded', () => {
  const mi = document.getElementById('midform-keyword-input');
  if (mi) mi.addEventListener('keydown', e => { if (e.key === 'Enter') startMidform(); });
  const ei = document.getElementById('edit-keyword-input');
  if (ei) ei.addEventListener('keydown', e => { if (e.key === 'Enter') startEditAnalysis(); });
});

function resetToMidform() {
  document.getElementById('midform-report-section').classList.add('hidden');
  document.getElementById('midform-progress-section').classList.add('hidden');
  document.getElementById('midform-input-section').classList.remove('hidden');
  document.getElementById('midform-btn').disabled = false;
  midformAnalyzing = false;
}

function startMidform() {
  if (midformAnalyzing) return;
  const keyword = document.getElementById('midform-keyword-input').value.trim();
  if (!keyword) { document.getElementById('midform-keyword-input').focus(); return; }
  const product_desc = document.getElementById('midform-product-input').value.trim();
  runMidform(keyword, product_desc);
}

async function runMidform(keyword, product_desc) {
  midformAnalyzing = true;
  document.getElementById('midform-btn').disabled = true;
  document.getElementById('midform-input-section').classList.add('hidden');
  document.getElementById('midform-report-section').classList.add('hidden');
  document.getElementById('midform-progress-steps').innerHTML = '';
  document.getElementById('midform-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('midform-progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/midform', { keyword, product_desc },
    addStep,
    (data) => {
      document.getElementById('midform-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('기획 완성!', 'done');
      setTimeout(() => {
        document.getElementById('midform-progress-section').classList.add('hidden');
        renderMidformReport(data.report, keyword);
        document.getElementById('midform-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        midformAnalyzing = false;
        document.getElementById('midform-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('midform-progress-steps').innerHTML = '';
      makeProgressStepper('midform-progress-steps')(msg, 'error');
      midformAnalyzing = false;
      document.getElementById('midform-btn').disabled = false;
    }
  );
}

function renderMidformReport(r, keyword) {
  document.getElementById('midform-report-title').textContent = `"${keyword}" 영상 기획 완성본`;

  // 콘텐츠 유형 배너
  const ct = r.content_type || '';
  const ctKey = ct.includes('+') ? '풀링+키' : ct.includes('키') ? '키' : ct ? '풀링' : '';
  const ctCfg = ctKey ? CTYPE_CONFIG[ctKey] : null;
  const ctBanner = document.getElementById('midform-content-type-banner');
  if (ctBanner) {
    if (ctCfg) {
      ctBanner.innerHTML = `<span style="background:${ctCfg.bg};color:${ctCfg.color};border:1.5px solid ${ctCfg.border};padding:4px 14px;border-radius:20px;font-size:14px;font-weight:700">${ctCfg.icon} ${ctCfg.label}</span>${r.content_type_reason ? `<span style="font-size:13px;color:#6b7280;margin-left:12px">${r.content_type_reason}</span>` : ''}${r.sell_angle ? `<span style="font-size:13px;color:#9a3412;margin-left:8px">💰 ${r.sell_angle}</span>` : ''}`;
      ctBanner.style.display = '';
    } else {
      ctBanner.style.display = 'none';
    }
  }

  const cb = document.getElementById('midform-concept');
  cb.textContent = r.concept || '';
  cb.style.display = r.concept ? '' : 'none';

  document.getElementById('midform-market-summary').textContent = r.market_summary || '';

  const desires = r.viewer_desires || {};
  renderList('midform-curiosity', desires.curiosity);
  renderList('midform-complaints', desires.complaints);
  renderList('midform-wants', desires.wants);

  // 제목
  const tg = document.getElementById('midform-titles');
  tg.innerHTML = '';
  (r.titles || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'title-card';
    div.innerHTML = `
      <div class="title-num">제목 ${i + 1}</div>
      <div class="title-text">${t.title || t}</div>
      ${t.strategy ? `<div class="title-hook" style="color:#f59e0b">전략: ${t.strategy}</div>` : ''}
      ${t.hook_reason ? `<div class="title-hook">${t.hook_reason}</div>` : ''}
    `;
    tg.appendChild(div);
  });

  // 썸네일
  const thg = document.getElementById('midform-thumbnails');
  thg.innerHTML = '';
  (r.thumbnails || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'thumb-concept-card';
    div.innerHTML = `
      <div class="thumb-concept-num">썸네일 ${i + 1}</div>
      <div class="thumb-main-text">"${t.main_text || ''}"</div>
      ${t.sub_text ? `<div class="thumb-sub-text">${t.sub_text}</div>` : ''}
      <div class="thumb-concept-detail"><strong>🎨 색상/분위기:</strong> ${t.color_mood || ''}</div>
      <div class="thumb-concept-detail"><strong>📸 이미지/구도:</strong> ${t.visual || ''}</div>
      ${t.expression ? `<div class="thumb-concept-detail"><strong>😊 표정/포즈:</strong> ${t.expression}</div>` : ''}
      <div class="thumb-concept-why">${t.why_clicks || ''}</div>
    `;
    thg.appendChild(div);
  });

  // 문제 정의
  const pd = r.problem_definition || {};
  document.getElementById('midform-problem').innerHTML = `
    <div class="problem-def-item">
      <div class="problem-def-label">📍 현재 시청자 상황</div>
      <div class="problem-def-text">${pd.viewer_situation || ''}</div>
    </div>
    <div class="problem-def-item">
      <div class="problem-def-label">✨ 시청자가 원하는 결과</div>
      <div class="problem-def-text">${pd.core_desire || ''}</div>
    </div>
    <div class="problem-def-item" style="grid-column:1/-1">
      <div class="problem-def-label">💡 이 영상의 해결 각도</div>
      <div class="problem-def-text" style="font-size:16px;font-weight:700;color:#1e293b">${pd.video_angle || ''}</div>
    </div>
  `;

  // 도입부
  const intro = r.intro || {};
  document.getElementById('midform-intro').innerHTML = `
    <div class="midform-intro-formula">
      <span class="midform-formula-badge">${intro.formula || ''}</span>
      <span class="midform-formula-reason">${intro.reason || ''}</span>
    </div>
    ${intro.hook_line ? `<div class="midform-hook-line">"${intro.hook_line}"</div>` : ''}
    <div class="intro-script-output" style="margin:12px 24px 20px">${intro.script || ''}</div>
  `;

  // 전체 원고
  const sectionsEl = document.getElementById('midform-script-sections');
  sectionsEl.innerHTML = '';
  (r.script_sections || []).forEach((s) => {
    const div = document.createElement('div');
    div.className = 'midform-section';
    div.innerHTML = `
      <div class="midform-section-header">
        <span class="midform-section-time">${s.timestamp || ''}</span>
        <span class="midform-section-name">${s.name || ''}</span>
      </div>
      <div class="midform-section-content">${s.content || ''}</div>
      <div class="midform-section-script">"${s.script || ''}"</div>
      ${s.filming_tip ? `<div class="midform-section-tip">💡 ${s.filming_tip}</div>` : ''}
    `;
    sectionsEl.appendChild(div);
  });

  // CTA
  document.getElementById('midform-cta').textContent = r.cta || '';

  // 반드시 넣어야 할 내용
  renderList('midform-must-include', r.must_include);

  // 차별화 포인트
  const dg = document.getElementById('midform-differentiation');
  dg.innerHTML = '';
  (r.differentiation || []).forEach(p => {
    const div = document.createElement('div');
    div.className = 'diff-card';
    div.textContent = p;
    dg.appendChild(div);
  });

  // 유튜브 설명글 / 인스타 캡션
  const ytDesc = document.getElementById('midform-yt-desc');
  if (ytDesc) ytDesc.textContent = r.youtube_description || '';
  const instaEl = document.getElementById('midform-insta-cap');
  if (instaEl) instaEl.textContent = r.instagram_caption || '';

  // 예상 길이
  const dur = document.getElementById('midform-duration');
  if (r.estimated_duration) {
    dur.textContent = r.estimated_duration;
    dur.style.display = '';
  } else {
    dur.style.display = 'none';
  }
}

// ===== 📱 숏폼 기획 =====

let _selectedDuration = '30';
let _currentShortformCaption = '';

function selectDuration(btn) {
  document.querySelectorAll('.duration-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _selectedDuration = btn.dataset.dur;
}

function resetToShortform() {
  document.getElementById('shortform-report-section').classList.add('hidden');
  document.getElementById('shortform-progress-section').classList.add('hidden');
  document.getElementById('shortform-input-section').classList.remove('hidden');
  document.getElementById('shortform-btn').disabled = false;
  shortformAnalyzing = false;
}

function startShortform() {
  if (shortformAnalyzing) return;
  const keyword = document.getElementById('shortform-keyword-input').value.trim();
  if (!keyword) { document.getElementById('shortform-keyword-input').focus(); return; }
  const product_desc = document.getElementById('shortform-product-input').value.trim();
  runShortform(keyword, product_desc, _selectedDuration);
}

async function runShortform(keyword, product_desc, duration) {
  shortformAnalyzing = true;
  document.getElementById('shortform-btn').disabled = true;
  document.getElementById('shortform-input-section').classList.add('hidden');
  document.getElementById('shortform-report-section').classList.add('hidden');
  document.getElementById('shortform-progress-steps').innerHTML = '';
  document.getElementById('shortform-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('shortform-progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/shortform', { keyword, product_desc, duration },
    addStep,
    (data) => {
      document.getElementById('shortform-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('기획 완성!', 'done');
      setTimeout(() => {
        document.getElementById('shortform-progress-section').classList.add('hidden');
        renderShortformReport(data.report, keyword);
        document.getElementById('shortform-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        shortformAnalyzing = false;
        document.getElementById('shortform-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('shortform-progress-steps').innerHTML = '';
      makeProgressStepper('shortform-progress-steps')(msg, 'error');
      shortformAnalyzing = false;
      document.getElementById('shortform-btn').disabled = false;
    }
  );
}

function renderShortformReport(r, keyword) {
  _currentShortformCaption = r.caption?.full_caption || '';
  document.getElementById('shortform-report-title').textContent = `"${keyword}" 릴스 기획안`;

  const cm = document.getElementById('shortform-core-message');
  cm.textContent = r.core_message || '';
  cm.style.display = r.core_message ? '' : 'none';

  const hooksEl = document.getElementById('shortform-hooks');
  hooksEl.innerHTML = '';
  (r.hooks || []).forEach((h, i) => {
    const div = document.createElement('div');
    div.className = 'shortform-hook-card';
    div.innerHTML = `
      <div class="shortform-hook-num">훅 ${i + 1} <span class="shortform-hook-type">${h.type || ''}</span></div>
      <div class="shortform-hook-text">"${h.text || ''}"</div>
      <div class="shortform-hook-why">${h.why || ''}</div>
    `;
    hooksEl.appendChild(div);
  });

  const scriptEl = document.getElementById('shortform-script');
  scriptEl.innerHTML = '';
  (r.script || []).forEach((s) => {
    const div = document.createElement('div');
    div.className = 'shortform-scene';
    div.innerHTML = `
      <div class="shortform-scene-time">${s.time || ''}</div>
      <div class="shortform-scene-body">
        <div class="shortform-scene-scene">🎥 ${s.scene || ''}</div>
        ${s.narration ? `<div class="shortform-scene-narration">🗣 "${s.narration}"</div>` : ''}
        ${s.text_overlay ? `<div class="shortform-scene-overlay">📝 자막: <strong>${s.text_overlay}</strong></div>` : ''}
        ${s.action ? `<div class="shortform-scene-action">💡 ${s.action}</div>` : ''}
      </div>
    `;
    scriptEl.appendChild(div);
  });

  renderList('shortform-save-triggers', r.save_triggers);
  renderList('shortform-share-triggers', r.share_triggers);

  const ctaEl = document.getElementById('shortform-comment-cta');
  const cta = r.comment_cta || {};
  ctaEl.innerHTML = `
    <div class="shortform-cta-question">"${cta.question || ''}"</div>
    <div class="shortform-cta-why">${cta.why_comments || ''}</div>
    ${(cta.alternatives || []).map((a, i) => `<div class="shortform-cta-alt">대안 ${i+1}: "${a}"</div>`).join('')}
  `;

  const coverEl = document.getElementById('shortform-cover-frame');
  const cf = r.cover_frame || {};
  coverEl.innerHTML = `
    <div class="shortform-cover-grid">
      <div class="shortform-cover-preview">
        <div class="shortform-cover-main">${cf.main_text || ''}</div>
        ${cf.sub_text ? `<div class="shortform-cover-sub">${cf.sub_text}</div>` : ''}
      </div>
      <div class="shortform-cover-info">
        <div class="shortform-cover-detail"><strong>📸 비주얼:</strong> ${cf.visual || ''}</div>
        <div class="shortform-cover-detail"><strong>✅ 클릭 이유:</strong> ${cf.why_clicks || ''}</div>
      </div>
    </div>
  `;

  const capEl = document.getElementById('shortform-caption');
  const cap = r.caption || {};
  capEl.innerHTML = `
    <div class="shortform-caption-box">
      <div class="shortform-caption-label">첫 줄 훅</div>
      <div class="shortform-caption-hook">${cap.hook_line || ''}</div>
    </div>
    <div class="shortform-caption-full">
      <div class="shortform-caption-label">완성 캡션 <button class="shortform-copy-mini" onclick="copyShortformCaption()">복사</button></div>
      <pre class="shortform-caption-text">${cap.full_caption || ''}</pre>
    </div>
  `;

  const hashEl = document.getElementById('shortform-hashtags');
  const ht = r.hashtags || {};
  const renderTags = (tags, cls) => (tags || []).map(t => `<span class="hash-tag ${cls}">${t}</span>`).join('');
  hashEl.innerHTML = `
    <div class="hash-group"><div class="hash-group-label">핵심 (검색량 높음)</div><div class="hash-tags">${renderTags(ht.core, 'hash-core')}</div></div>
    <div class="hash-group"><div class="hash-group-label">틈새 (경쟁 낮음)</div><div class="hash-tags">${renderTags(ht.niche, 'hash-niche')}</div></div>
    <div class="hash-group"><div class="hash-group-label">트렌딩</div><div class="hash-tags">${renderTags(ht.trending, 'hash-trending')}</div></div>
    ${ht.strategy ? `<div class="hash-strategy">${ht.strategy}</div>` : ''}
  `;

  renderList('shortform-text-overlay', r.text_overlay_guide);

  const mlEl = document.getElementById('shortform-music-loop');
  mlEl.innerHTML = `
    <div style="margin-bottom:12px"><strong>🎵 음악:</strong> ${r.music_mood || ''}</div>
    <div><strong>🔁 루프 팁:</strong> ${r.loop_tip || ''}</div>
  `;
}

function copyText(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    const btn = el.previousElementSibling?.querySelector('.shortform-copy-mini') ||
                el.parentElement?.querySelector('.shortform-copy-mini');
    if (btn) { btn.textContent = '✅ 복사됨!'; setTimeout(() => btn.textContent = '복사', 2000); }
  }).catch(() => alert('복사 실패'));
}

function copyShortformCaption() {
  if (!_currentShortformCaption) return;
  navigator.clipboard.writeText(_currentShortformCaption).then(() => {
    const btns = document.querySelectorAll('#shortform-report-section .copy-btn, .shortform-copy-mini');
    btns.forEach(btn => { btn.textContent = '✅ 복사됨!'; setTimeout(() => btn.textContent = btn.classList.contains('copy-btn') ? '📋 캡션 복사' : '복사', 2000); });
  }).catch(() => alert('복사 실패'));
}

// ===== ✏️ 편집 피드백 =====

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

// ===== 📚 히스토리 =====

async function loadHistory(type) {
  ['all', 'topic', 'midform', 'shortform', 'edit'].forEach(t => {
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

  const typeLabels = {
    topic: '주제 추천', midform: '미드폼', shortform: '숏폼', edit: '편집 피드백',
    research: '시장조사', planning: '기획', intro: '도입부', script: '대본'
  };
  const typeColors = {
    topic: '#ef4444', midform: '#3b82f6', shortform: '#ec4899', edit: '#8b5cf6',
    research: '#6366f1', planning: '#f59e0b', intro: '#10b981', script: '#ef4444'
  };

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
    topic: () => {
      switchTab('topic'); resetToTopic();
      setTimeout(() => {
        renderTopicReport(data.report);
        document.getElementById('topic-report-section').classList.remove('hidden');
        document.getElementById('topic-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    midform: () => {
      switchTab('midform'); resetToMidform();
      setTimeout(() => {
        renderMidformReport(data.report, data.keyword);
        document.getElementById('midform-report-section').classList.remove('hidden');
        document.getElementById('midform-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    shortform: () => {
      switchTab('shortform'); resetToShortform();
      setTimeout(() => {
        renderShortformReport(data.report, data.keyword);
        document.getElementById('shortform-report-section').classList.remove('hidden');
        document.getElementById('shortform-input-section').classList.add('hidden');
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
    // legacy history items
    research: () => {
      switchTab('research');
      setTimeout(() => {
        renderReport(data.report, data.keyword);
        document.getElementById('report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    planning: () => {
      switchTab('planning');
      setTimeout(() => {
        renderPlanningReport(data.report, data.keyword);
        document.getElementById('planning-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    intro: () => {
      switchTab('intro');
      setTimeout(() => {
        renderIntroReport(data.report, data.keyword);
        document.getElementById('intro-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    script: () => {
      switchTab('script');
      setTimeout(() => {
        renderScriptReport(data.report, data.keyword);
        document.getElementById('script-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
  };

  (actions[data.type] || actions.midform)();
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

// ===== 하위호환 렌더 함수 (이전 히스토리 항목용) =====

let _currentReport = null;
let _currentKeyword = '';

function renderReport(r, keyword) {
  _currentReport = r; _currentKeyword = keyword;
  document.getElementById('report-keyword-title').textContent = `"${keyword}" 시장조사 결과`;
  document.getElementById('report-subtitle').textContent = '유튜브 + 네이버 데이터 기반';
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
  const vl = document.getElementById('top-videos-list');
  vl.innerHTML = '';
  (r.top_videos || []).forEach(v => {
    const card = document.createElement('div');
    card.className = 'video-thumb-card';
    card.innerHTML = `<a href="${v.url}" target="_blank" rel="noopener"><img src="${v.thumbnail}" alt="${v.title}" loading="lazy" /><div class="video-thumb-info"><div class="video-thumb-title">${v.title}</div><div class="video-thumb-views">조회수 ${fmt(v.views)}회 · ${v.channel}</div></div></a>`;
    vl.appendChild(card);
  });
}

function renderPlanningReport(r, keyword) {
  document.getElementById('planning-report-title').textContent = `"${keyword}" 기획안`;
  const pd = r.problem_definition || {};
  document.getElementById('planning-problem').innerHTML = `
    <div class="problem-def-item"><div class="problem-def-label">현상</div><div class="problem-def-text">${pd.current_situation || ''}</div></div>
    <div class="problem-def-item"><div class="problem-def-label">욕구</div><div class="problem-def-text">${pd.desired_outcome || ''}</div></div>
    <div class="problem-def-item" style="grid-column:1/-1"><div class="problem-def-label">핵심 문제</div><div class="problem-def-text">${pd.core_problem || ''}</div></div>
    <div class="problem-def-item" style="grid-column:1/-1"><div class="problem-def-label">해결 각도</div><div class="problem-def-text">${pd.solution_angle || ''}</div></div>
  `;
  const tg = document.getElementById('planning-titles');
  tg.innerHTML = '';
  (r.recommended_titles || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'title-card';
    div.innerHTML = `<div class="title-num">제목 ${i+1}</div><div class="title-text">${t.title || t}</div>${t.hook_reason ? `<div class="title-hook">${t.hook_reason}</div>` : ''}`;
    tg.appendChild(div);
  });
  const thg = document.getElementById('planning-thumbnails');
  thg.innerHTML = '';
  (r.thumbnail_concepts || []).forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'thumb-concept-card';
    div.innerHTML = `<div class="thumb-concept-num">썸네일 ${i+1}</div><div class="thumb-main-text">"${t.main_text||''}"</div><div class="thumb-concept-detail">${t.visual||''}</div>`;
    thg.appendChild(div);
  });
}

let _currentIntroScript = '';
function renderIntroReport(r, keyword) {
  _currentIntroScript = r.full_intro || '';
  document.getElementById('intro-report-title').textContent = `"${keyword}" 도입부`;
  document.getElementById('intro-full-script').textContent = r.full_intro || '';
  const breakdownEl = document.getElementById('intro-breakdown');
  breakdownEl.innerHTML = '';
  (r.breakdown || []).forEach(item => {
    const div = document.createElement('div');
    div.className = 'intro-breakdown-item';
    div.innerHTML = `<div class="intro-stage-badge">${item.stage} · ${item.duration_sec || ''}초</div><div class="intro-stage-script">"${item.text||''}"</div><div class="intro-stage-purpose">${item.purpose||''}</div>`;
    breakdownEl.appendChild(div);
  });
}

let _currentAdaptedScript = '';
function renderScriptReport(r, keyword) {
  _currentAdaptedScript = r.adapted_script || '';
  document.getElementById('script-report-title').textContent = `"${keyword}" 변형 대본`;
  document.getElementById('script-adapted').textContent = r.adapted_script || '';
}

// ===== 📋 업로드 결정 =====

let decisionAnalyzing = false;
let decisionVideoCount = 0;

function addDecisionVideo() {
  const list = document.getElementById('decision-video-list');
  if (decisionVideoCount >= 8) { alert('최대 8개까지 추가할 수 있습니다.'); return; }
  decisionVideoCount++;
  const idx = decisionVideoCount;
  const card = document.createElement('div');
  card.className = 'decision-video-card';
  card.id = `decision-video-${idx}`;
  card.innerHTML = `
    <div class="decision-video-header">
      <span class="decision-video-num">#${idx}</span>
      <button class="decision-remove-btn" onclick="removeDecisionVideo(${idx})">✕ 삭제</button>
    </div>
    <input type="text" placeholder="제목 아이디어 (예: 업소용 가스레인지 청소 꿀팁)" class="decision-input" id="dv-title-${idx}" />
    <textarea placeholder="영상 내용 설명 (어떤 내용을 다루는지, 어떤 제품/주제인지)" class="decision-textarea" rows="2" id="dv-desc-${idx}"></textarea>
    <textarea placeholder="대본 또는 아웃라인 (선택 — 있으면 더 정확하게 분석)" class="decision-textarea" rows="3" id="dv-script-${idx}"></textarea>
    <input type="text" placeholder="썸네일 컨셉 (선택 — 어떤 이미지/문구를 생각 중인지)" class="decision-input" id="dv-thumb-${idx}" />
  `;
  list.appendChild(card);
}

function removeDecisionVideo(idx) {
  const card = document.getElementById(`decision-video-${idx}`);
  if (card) card.remove();
}

function resetToDecision() {
  document.getElementById('decision-report-section').classList.add('hidden');
  document.getElementById('decision-progress-section').classList.add('hidden');
  document.getElementById('decision-input-section').classList.remove('hidden');
  document.getElementById('decision-btn').disabled = false;
  decisionAnalyzing = false;
}

function startVideoDecision() {
  if (decisionAnalyzing) return;

  const cards = document.querySelectorAll('.decision-video-card');
  if (cards.length === 0) { alert('영상을 하나 이상 추가해주세요.'); return; }

  const videos = [];
  let hasContent = false;
  cards.forEach(card => {
    const id = card.id.replace('decision-video-', '');
    const title = (document.getElementById(`dv-title-${id}`) || {}).value || '';
    const description = (document.getElementById(`dv-desc-${id}`) || {}).value || '';
    const script = (document.getElementById(`dv-script-${id}`) || {}).value || '';
    const thumbnail_concept = (document.getElementById(`dv-thumb-${id}`) || {}).value || '';
    if (title.trim() || description.trim()) hasContent = true;
    videos.push({ title, description, script, thumbnail_concept });
  });

  if (!hasContent) { alert('최소 하나의 영상에 제목 또는 내용을 입력해주세요.'); return; }

  decisionAnalyzing = true;
  document.getElementById('decision-btn').disabled = true;
  document.getElementById('decision-input-section').classList.add('hidden');
  document.getElementById('decision-report-section').classList.add('hidden');
  document.getElementById('decision-progress-steps').innerHTML = '';
  document.getElementById('decision-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('decision-progress-steps');
  addStep(`영상 ${videos.length}개 분석 준비 중...`, 'active');

  streamSSE(
    '/api/video-decision',
    { videos },
    addStep,
    (data) => {
      document.getElementById('decision-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('분석 완료!', 'done');
      setTimeout(() => {
        document.getElementById('decision-progress-section').classList.add('hidden');
        renderDecisionReport(data.report);
        document.getElementById('decision-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        decisionAnalyzing = false;
        document.getElementById('decision-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('decision-progress-steps').innerHTML = '';
      makeProgressStepper('decision-progress-steps')(msg, 'error');
      decisionAnalyzing = false;
      document.getElementById('decision-btn').disabled = false;
      document.getElementById('decision-input-section').classList.remove('hidden');
      document.getElementById('decision-progress-section').classList.add('hidden');
    }
  );
}

const SCORE_COLOR = (s) => s >= 80 ? '#10b981' : s >= 60 ? '#f59e0b' : '#ef4444';
const RANK_MEDAL = ['🥇', '🥈', '🥉'];

function renderDecisionReport(r) {
  document.getElementById('decision-schedule').textContent = r.upload_schedule || '';
  document.getElementById('decision-strategy').textContent = r.overall_strategy || '';

  const el = document.getElementById('decision-ranking');
  el.innerHTML = '';
  (r.ranking || []).forEach(item => {
    const medal = RANK_MEDAL[item.rank - 1] || `#${item.rank}`;
    const score = item.performance_score || 0;
    const div = document.createElement('div');
    div.className = 'decision-rank-card';
    div.innerHTML = `
      <div class="decision-rank-header">
        <span class="decision-rank-medal">${medal}</span>
        <span class="decision-rank-title">${item.original_title || `영상 ${item.video_index}`}</span>
        <span class="decision-rank-score" style="color:${SCORE_COLOR(score)}">${score}점</span>
      </div>
      <div class="decision-rank-body">
        <div class="decision-rank-section"><strong>📅 업로드 타이밍</strong><p>${item.timing_recommendation || ''}</p></div>
        <div class="decision-rank-section"><strong>💡 추천 이유</strong><p>${item.reason || ''}</p></div>
        <div class="decision-rank-section decision-improved">
          <strong>✍️ 개선 제목</strong><p class="decision-improved-title">${item.improved_title || ''}</p>
        </div>
        <div class="decision-rank-section"><strong>🖼️ 썸네일 팁</strong><p>${item.thumbnail_tip || ''}</p></div>
        ${item.risk ? `<div class="decision-rank-section decision-risk"><strong>⚠️ 주의</strong><p>${item.risk}</p></div>` : ''}
      </div>
    `;
    el.appendChild(div);
  });
}

// ===== 📊 채널 분석 =====

let channelAnalyzing = false;

function resetToChannel() {
  document.getElementById('channel-report-section').classList.add('hidden');
  document.getElementById('channel-progress-section').classList.add('hidden');
  document.getElementById('channel-input-section').classList.remove('hidden');
  document.getElementById('channel-btn').disabled = false;
  channelAnalyzing = false;
}

function startChannelAnalyze() {
  if (channelAnalyzing) return;
  const channelId = document.getElementById('channel-id-input').value.trim();
  if (!channelId) { alert('채널 ID를 입력해주세요.'); return; }

  channelAnalyzing = true;
  document.getElementById('channel-btn').disabled = true;
  document.getElementById('channel-input-section').classList.add('hidden');
  document.getElementById('channel-report-section').classList.add('hidden');
  document.getElementById('channel-progress-steps').innerHTML = '';
  document.getElementById('channel-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('channel-progress-steps');
  addStep('채널 정보 불러오는 중...', 'active');

  streamSSE(
    '/api/channel-analyze',
    { channel_id: channelId },
    addStep,
    (data) => {
      document.getElementById('channel-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('분석 완료!', 'done');
      setTimeout(() => {
        document.getElementById('channel-progress-section').classList.add('hidden');
        renderChannelReport(data.report);
        document.getElementById('channel-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        channelAnalyzing = false;
        document.getElementById('channel-btn').disabled = false;
      }, 600);
    },
    (msg) => {
      document.getElementById('channel-progress-steps').innerHTML = '';
      makeProgressStepper('channel-progress-steps')(msg, 'error');
      channelAnalyzing = false;
      document.getElementById('channel-btn').disabled = false;
      document.getElementById('channel-input-section').classList.remove('hidden');
      document.getElementById('channel-progress-section').classList.add('hidden');
    }
  );
}

function renderChannelReport(r) {
  const ci = r.channel_info || {};
  document.getElementById('channel-report-title').textContent = ci.title ? `${ci.title} 채널 분석` : '채널 분석 결과';
  document.getElementById('channel-report-subtitle').textContent =
    ci.subscriber_count ? `구독자 ${ci.subscriber_count.toLocaleString()}명 · 영상 ${r.total_analyzed || 0}개 분석` : '';

  const summary = document.getElementById('channel-summary');
  summary.textContent = r.channel_summary || '';
  summary.style.display = r.channel_summary ? '' : 'none';

  // 잘되는 주제
  const topEl = document.getElementById('channel-top-topics');
  topEl.innerHTML = '';
  (r.top_performing_topics || []).forEach(t => {
    const d = document.createElement('div');
    d.className = 'ch-topic-item ch-topic-good';
    d.innerHTML = `<div class="ch-topic-name">${t.topic}</div>
      <div class="ch-topic-views">평균 ${(t.avg_views || 0).toLocaleString()}회</div>
      <div class="ch-topic-reason">${t.reason}</div>
      ${t.example ? `<div class="ch-topic-example">예: ${t.example}</div>` : ''}`;
    topEl.appendChild(d);
  });

  // 안되는 주제
  const underEl = document.getElementById('channel-under-topics');
  underEl.innerHTML = '';
  (r.underperforming_topics || []).forEach(t => {
    const d = document.createElement('div');
    d.className = 'ch-topic-item ch-topic-bad';
    d.innerHTML = `<div class="ch-topic-name">${t.topic}</div>
      <div class="ch-topic-views">평균 ${(t.avg_views || 0).toLocaleString()}회</div>
      <div class="ch-topic-reason">${t.reason}</div>`;
    underEl.appendChild(d);
  });

  // 최적 요일
  const daysEl = document.getElementById('channel-best-days');
  daysEl.innerHTML = (r.best_upload_days || []).map((d, i) =>
    `<div class="ch-day-item ${i === 0 ? 'ch-day-best' : ''}">${i === 0 ? '🥇' : '🥈'} ${d}</div>`
  ).join('') + (r.worst_upload_days || []).map(d =>
    `<div class="ch-day-item ch-day-worst">🚫 ${d}</div>`
  ).join('');

  // 최적 시간대
  const hoursEl = document.getElementById('channel-best-hours');
  hoursEl.innerHTML = (r.best_upload_hours || []).map(h =>
    `<div class="ch-day-item">${h}</div>`
  ).join('');

  // 최적 영상 길이
  document.getElementById('channel-optimal-length').textContent = r.optimal_video_length || '';

  // 제목 패턴
  const patternsEl = document.getElementById('channel-title-patterns');
  patternsEl.innerHTML = '';
  (r.successful_title_patterns || []).forEach(p => {
    const d = document.createElement('div');
    d.className = 'ch-pattern-item';
    d.innerHTML = `<div class="ch-pattern-name">${p.pattern}</div>
      <div class="ch-pattern-example">"${p.example}"</div>
      <div class="ch-pattern-why">${p.why}</div>`;
    patternsEl.appendChild(d);
  });

  // 병목
  document.getElementById('channel-bottleneck').textContent = r.growth_bottleneck || '';

  // 개선 방안
  const recEl = document.getElementById('channel-recommendations');
  recEl.innerHTML = '';
  (r.channel_recommendations || []).forEach(rec => {
    const li = document.createElement('li');
    li.textContent = rec;
    recEl.appendChild(li);
  });

  // 다음 영상 전략
  document.getElementById('channel-next-strategy').textContent = r.next_video_strategy || '';
}

// ===== 💬 AI 상담 =====

let chatHistory = [];
let chatSending = false;
let chatAttachments = [];

const CHAT_WELCOME = `<div class="chat-bubble assistant">
  <div class="chat-bubble-inner">
    안녕하세요! 부자주방 채널 전담 콘텐츠 전략 파트너입니다. 👋<br><br>
    미드폼·숏폼 기획, 제목·썸네일 전략, 업로드 타이밍, 채널 성장까지 궁금한 것은 무엇이든 물어보세요.<br><br>
    채널 목표(CTR 10%+, 30초 이탈률 40% 미만)와 풀링·키 콘텐츠 전략을 기반으로 구체적으로 답변드립니다.
    <div class="chat-suggestion-chips">
      <span class="chat-chip" onclick="sendChatChip(this)">릴스 훅 잡는 법</span>
      <span class="chat-chip" onclick="sendChatChip(this)">조회수 오르는 제목 공식</span>
      <span class="chat-chip" onclick="sendChatChip(this)">풀링 vs 키 콘텐츠 차이</span>
      <span class="chat-chip" onclick="sendChatChip(this)">업로드 최적 요일·시간</span>
    </div>
  </div>
</div>`;

function clearChat() {
  chatHistory = [];
  chatAttachments = [];
  document.getElementById('chat-messages').innerHTML = CHAT_WELCOME;
  document.getElementById('chat-attach-preview').innerHTML = '';
  document.getElementById('chat-attach-preview').classList.add('hidden');
}

function handleChatFiles(input) {
  const preview = document.getElementById('chat-attach-preview');
  const files = Array.from(input.files);
  if (!files.length) return;

  const toRead = files.map(file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = e => {
      const dataUrl = e.target.result;
      const [header, data] = dataUrl.split(',');
      const media_type = header.match(/:(.*?);/)[1];
      resolve({ name: file.name, media_type, data });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  }));

  Promise.all(toRead).then(results => {
    chatAttachments.push(...results);
    preview.classList.remove('hidden');
    preview.innerHTML = chatAttachments.map((a, i) => {
      if (a.media_type.startsWith('image/')) {
        return `<div class="attach-thumb-wrap">
          <img class="attach-thumb" src="data:${a.media_type};base64,${a.data}" title="${a.name}" />
          <button class="attach-remove" onclick="removeChatAttach(${i})">✕</button>
        </div>`;
      }
      return `<div class="attach-thumb-wrap">
        <div class="attach-pdf-badge">📄 ${a.name}</div>
        <button class="attach-remove" onclick="removeChatAttach(${i})">✕</button>
      </div>`;
    }).join('');
  });

  input.value = '';
}

function removeChatAttach(idx) {
  chatAttachments.splice(idx, 1);
  const preview = document.getElementById('chat-attach-preview');
  if (!chatAttachments.length) {
    preview.innerHTML = '';
    preview.classList.add('hidden');
    return;
  }
  preview.innerHTML = chatAttachments.map((a, i) => {
    if (a.media_type.startsWith('image/')) {
      return `<div class="attach-thumb-wrap">
        <img class="attach-thumb" src="data:${a.media_type};base64,${a.data}" title="${a.name}" />
        <button class="attach-remove" onclick="removeChatAttach(${i})">✕</button>
      </div>`;
    }
    return `<div class="attach-thumb-wrap">
      <div class="attach-pdf-badge">📄 ${a.name}</div>
      <button class="attach-remove" onclick="removeChatAttach(${i})">✕</button>
    </div>`;
  }).join('');
}

function sendChatChip(el) {
  const input = document.getElementById('chat-input');
  input.value = el.textContent;
  sendChat();
}

function chatInputResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function chatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
}

function _escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _formatChat(text) {
  return _escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
}

async function sendChat() {
  if (chatSending) return;
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message && !chatAttachments.length) return;

  chatSending = true;
  document.getElementById('chat-send-btn').disabled = true;
  input.value = '';
  input.style.height = 'auto';

  const attachmentsToSend = [...chatAttachments];
  chatAttachments = [];
  const preview = document.getElementById('chat-attach-preview');
  preview.innerHTML = '';
  preview.classList.add('hidden');

  const messages = document.getElementById('chat-messages');

  // 사용자 메시지 추가
  const userBubble = document.createElement('div');
  userBubble.className = 'chat-bubble user';
  let thumbsHtml = attachmentsToSend.map(a => {
    if (a.media_type.startsWith('image/')) {
      return `<img class="attach-thumb sent" src="data:${a.media_type};base64,${a.data}" />`;
    }
    return `<div class="attach-pdf-badge sent">📄 ${a.name}</div>`;
  }).join('');
  userBubble.innerHTML = `<div class="chat-bubble-inner">${thumbsHtml ? `<div class="bubble-attachments">${thumbsHtml}</div>` : ''}${message ? _escapeHtml(message) : ''}</div>`;
  messages.appendChild(userBubble);

  // 로딩 버블
  const aiBubble = document.createElement('div');
  aiBubble.className = 'chat-bubble assistant';
  aiBubble.innerHTML = `<div class="chat-bubble-inner"><div class="chat-typing"><span></span><span></span><span></span></div></div>`;
  messages.appendChild(aiBubble);
  messages.scrollTop = messages.scrollHeight;

  let fullText = '';
  const inner = aiBubble.querySelector('.chat-bubble-inner');

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history: chatHistory, attachments: attachmentsToSend }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let started = false;

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
          if (data.token) {
            if (!started) { inner.innerHTML = ''; started = true; }
            fullText += data.token;
            inner.innerHTML = _formatChat(fullText);
            messages.scrollTop = messages.scrollHeight;
          }
          if (data.done) {
            chatHistory.push({ role: 'user', content: message });
            chatHistory.push({ role: 'assistant', content: fullText });
          }
          if (data.error) {
            inner.innerHTML = `<span style="color:var(--red)">오류: ${_escapeHtml(data.error)}</span>`;
          }
        } catch (e) {}
      }
    }
  } catch (err) {
    inner.innerHTML = `<span style="color:var(--red)">연결 오류. 다시 시도해주세요.</span>`;
  }

  chatSending = false;
  document.getElementById('chat-send-btn').disabled = false;
  input.focus();
}
