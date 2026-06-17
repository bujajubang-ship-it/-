let midformAnalyzing = false;
let shortformAnalyzing = false;
let editAnalyzing = false;
let editChatHistory = [];
let editChatSending = false;
let editChatAttachments = [];
// kept for history backwards compatibility
let analyzing = false;
let planningAnalyzing = false;
let introAnalyzing = false;
let scriptAnalyzing = false;

const ALL_TABS = ['midform', 'shortform', 'topic', 'detail', 'edit', 'sns', 'decision', 'channel', 'blog', 'video-feedback', 'chat', 'history', 'pipeline', 'research', 'planning', 'intro', 'script'];

function switchTab(tab) {
  ALL_TABS.forEach(t => {
    const btn = document.getElementById(`tab-${t}`);
    if (btn) btn.classList.toggle('active', t === tab);
    const pane = document.getElementById(`pane-${t}`);
    if (pane) pane.classList.toggle('hidden', t !== tab);
  });
  if (tab === 'history') loadHistory('');
  if (tab === 'pipeline') loadPipeline();
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
  const product_url  = (document.getElementById('midform-product-url')?.value || '').trim();
  runMidform(keyword, product_desc, product_url);
}

async function runMidform(keyword, product_desc, product_url = '') {
  midformAnalyzing = true;
  document.getElementById('midform-btn').disabled = true;
  document.getElementById('midform-input-section').classList.add('hidden');
  document.getElementById('midform-report-section').classList.add('hidden');
  document.getElementById('midform-progress-steps').innerHTML = '';
  document.getElementById('midform-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('midform-progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/midform', { keyword, product_desc, product_url },
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

  // 벤치마크 영상
  const bv = r.benchmark_video;
  const bmEl = document.getElementById('midform-benchmark');
  if (bmEl) {
    if (bv && bv.title) {
      bmEl.style.display = '';
      bmEl.innerHTML = `
        <div class="benchmark-label">📊 벤치마크 영상</div>
        <div class="benchmark-title">${bv.title}</div>
        <div class="benchmark-detail"><strong>선택 이유:</strong> ${bv.reason || ''}</div>
        <div class="benchmark-detail"><strong>썸네일 패턴:</strong> ${bv.thumbnail_pattern || ''}</div>
        <div class="benchmark-our"><strong>부자주방 버전:</strong> ${bv.our_version || ''}</div>
      `;
    } else {
      bmEl.style.display = 'none';
    }
  }

  // 제목
  const tg = document.getElementById('midform-titles');
  tg.innerHTML = '';
  (r.titles || []).forEach((t, i) => {
    const typeLabel = t.title_keyword_type ? `<span class="title-keyword-type ${t.title_keyword_type}">${t.title_keyword_type}</span>` : '';
    const div = document.createElement('div');
    div.className = 'title-card';
    div.innerHTML = `
      <div class="title-num">제목 ${i + 1} ${typeLabel}</div>
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
    const etLabel = t.emotion_type ? `<span class="thumb-emotion-badge">${t.emotion_type}</span>` : '';
    const div = document.createElement('div');
    div.className = 'thumb-concept-card';
    div.innerHTML = `
      <div class="thumb-concept-num">썸네일 ${i + 1} ${etLabel}</div>
      <div class="thumb-main-text">"${t.main_text || ''}"</div>
      ${t.sub_text ? `<div class="thumb-sub-text">${t.sub_text}</div>` : ''}
      ${t.zoom_subject ? `<div class="thumb-concept-detail"><strong>🔍 줌 피사체:</strong> ${t.zoom_subject}</div>` : ''}
      ${t.visual_evidence ? `<div class="thumb-concept-detail"><strong>📷 시각적 근거:</strong> ${t.visual_evidence}</div>` : ''}
      ${t.target_image_style ? `<div class="thumb-concept-detail"><strong>👥 타겟 이미지 스타일:</strong> ${t.target_image_style}</div>` : ''}
      <div class="thumb-concept-detail"><strong>🎬 촬영 방법:</strong> ${t.visual || ''}</div>
      <div class="thumb-concept-detail"><strong>🎨 색상/분위기:</strong> ${t.color_mood || ''}</div>
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
    ${intro.thumbnail_callback ? `<div class="intro-callback-box"><span class="intro-callback-label">📌 썸네일 콜백</span>${intro.thumbnail_callback}</div>` : ''}
    ${intro.impact_scene_first ? `<div class="intro-impact-box"><span class="intro-impact-label">⚡ 앞으로 당길 임팩트 장면</span>${intro.impact_scene_first}</div>` : ''}
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

  // ViewTrap 인사이트
  const vtCard = document.getElementById('midform-viewtrap-card');
  const vtInsightsEl = document.getElementById('midform-viewtrap-insights');
  const vtTabsEl = document.getElementById('midform-viewtrap-tabs');
  const vtVideosEl = document.getElementById('midform-viewtrap-videos');
  const vti = r.viewtrap_insights;
  const vtTop = r.viewtrap_top || [];
  const vtHot = r.viewtrap_hot || [];
  const hasVt = (vti && (vti.applied_patterns || (vti.referenced_videos && vti.referenced_videos.length))) || vtTop.length || vtHot.length;
  if (vtCard && hasVt) {
    vtCard.style.display = '';
    // 인사이트 텍스트
    let insHtml = '';
    if (vti && vti.applied_patterns) insHtml += `<div class="vt-applied">${vti.applied_patterns}</div>`;
    if (vti && vti.referenced_videos && vti.referenced_videos.length) {
      insHtml += `<div class="vt-refs-label">AI가 참고한 영상 패턴</div><ul class="vt-refs-list">`;
      vti.referenced_videos.forEach(t => { insHtml += `<li>${t}</li>`; });
      insHtml += '</ul>';
    }
    if (vtInsightsEl) vtInsightsEl.innerHTML = insHtml;

    // 탭 + 영상 그리드
    function renderVtVideos(list) {
      if (!vtVideosEl) return;
      vtVideosEl.innerHTML = '';
      list.forEach(v => {
        const card = document.createElement('a');
        card.href = v.url || '#';
        card.target = '_blank';
        card.rel = 'noopener';
        card.className = 'vt-video-card';
        const perf = v.performance_rate_str || '';
        const perfClass = perf === '매우 좋음' || perf === '좋음' ? 'perf-good' : perf === '보통' ? 'perf-avg' : 'perf-bad';
        card.innerHTML = `
          <img src="${v.thumbnail || ''}" alt="" loading="lazy" class="vt-thumb" />
          <div class="vt-info">
            <div class="vt-title">${v.title || ''}</div>
            <div class="vt-meta"><span class="vt-channel">${v.channel || ''}</span><span class="vt-perf ${perfClass}">${perf}</span></div>
          </div>`;
        vtVideosEl.appendChild(card);
      });
    }

    if (vtTabsEl) {
      vtTabsEl.innerHTML = '';
      if (vtTop.length) {
        const btn1 = document.createElement('button');
        btn1.className = 'vt-tab active';
        btn1.textContent = `성과 영상 ${vtTop.length}개`;
        btn1.onclick = () => { vtTabsEl.querySelectorAll('.vt-tab').forEach(b=>b.classList.remove('active')); btn1.classList.add('active'); renderVtVideos(vtTop); };
        vtTabsEl.appendChild(btn1);
      }
      if (vtHot.length) {
        const btn2 = document.createElement('button');
        btn2.className = 'vt-tab';
        btn2.textContent = `신규 핫비디오 ${vtHot.length}개`;
        btn2.onclick = () => { vtTabsEl.querySelectorAll('.vt-tab').forEach(b=>b.classList.remove('active')); btn2.classList.add('active'); renderVtVideos(vtHot); };
        vtTabsEl.appendChild(btn2);
      }
    }
    renderVtVideos(vtTop.length ? vtTop : vtHot);
  } else if (vtCard) {
    vtCard.style.display = 'none';
  }

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

// ===== 🛒 상세페이지 기획 =====

function resetDetailPage() {
  document.getElementById('detail-report-section').classList.add('hidden');
  document.getElementById('detail-progress-section').classList.add('hidden');
  document.getElementById('detail-input-section').classList.remove('hidden');
  document.getElementById('detail-btn').disabled = false;
}

async function startDetailPage() {
  const keyword = document.getElementById('detail-keyword').value.trim();
  if (!keyword) { alert('제품 키워드를 입력해주세요.'); return; }

  const btn = document.getElementById('detail-btn');
  btn.disabled = true;
  document.getElementById('detail-input-section').classList.add('hidden');
  document.getElementById('detail-report-section').classList.add('hidden');

  const progressSection = document.getElementById('detail-progress-section');
  const progressSteps = document.getElementById('detail-progress-steps');
  progressSection.classList.remove('hidden');
  progressSteps.innerHTML = '';

  function addStep(msg, type = 'active') {
    const old = progressSteps.querySelector('.step.active');
    if (old) old.className = 'step done';
    const el = document.createElement('div');
    el.className = `step ${type}`;
    el.innerHTML = `<span class="step-icon">${type === 'done' ? '✅' : type === 'error' ? '❌' : '⏳'}</span><span>${msg}</span>`;
    progressSteps.appendChild(el);
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return el;
  }

  let buffer = '';
  try {
    const resp = await fetch('/api/detail-page', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        keyword,
        product_desc: document.getElementById('detail-product-desc').value.trim(),
        price: document.getElementById('detail-price').value.trim(),
        target_customer: document.getElementById('detail-target').value.trim(),
      }),
    });

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
          if (data.step === 'ping') continue;
          if (data.step === 'error') { addStep(data.message, 'error'); btn.disabled = false; return; }
          if (data.step === 'done') {
            progressSteps.querySelectorAll('.step.active').forEach(s => s.className = 'step done');
            addStep('기획안 완성!', 'done');
            setTimeout(() => {
              progressSection.classList.add('hidden');
              renderDetailPageReport(data.report, data.keyword);
            }, 600);
          } else {
            addStep(data.message || data.step);
          }
        } catch {}
      }
    }
  } catch (e) {
    addStep(`오류: ${e.message}`, 'error');
    btn.disabled = false;
  }
}

function renderDetailPageReport(r, keyword) {
  const now = new Date().toLocaleDateString('ko-KR');
  document.getElementById('detail-report-title').textContent = `${keyword} 상세페이지 기획안`;
  document.getElementById('detail-report-subtitle').textContent = `${now} 기준 시장 데이터 분석`;

  const body = document.getElementById('detail-report-body');
  body.innerHTML = '';

  // 시장 요약
  if (r.market_summary) {
    body.innerHTML += `<div class="report-card"><h3>📊 시장 분석 요약</h3><p>${r.market_summary}</p></div>`;
  }

  // 고객 페인포인트 + 구매 트리거
  const grid1 = `
    <div class="report-grid-2">
      <div class="report-card">
        <h3>😤 고객 페인포인트</h3>
        <ul>${(r.customer_pain_points || []).map(t => `<li>${t}</li>`).join('')}</ul>
      </div>
      <div class="report-card">
        <h3>💡 구매 결정 요인</h3>
        <ul>${(r.purchase_triggers || []).map(t => `<li>${t}</li>`).join('')}</ul>
      </div>
    </div>`;
  body.innerHTML += grid1;

  // 경쟁 제품 패턴
  if (r.competitor_patterns?.length) {
    body.innerHTML += `<div class="report-card"><h3>🔍 잘 팔리는 유사 제품 상세페이지 패턴</h3><ul>${r.competitor_patterns.map(t => `<li>${t}</li>`).join('')}</ul></div>`;
  }

  // 핵심 카피
  if (r.key_copies) {
    const kc = r.key_copies;
    body.innerHTML += `
      <div class="report-card detail-copy-card">
        <h3>✍️ 핵심 카피</h3>
        <div class="copy-block main-headline">"${kc.main_headline || ''}"</div>
        <div class="copy-label">서브 헤드라인</div>
        <div class="copy-block">"${kc.sub_headline || ''}"</div>
        <div class="copy-label">공감 오프너</div>
        <div class="copy-block empathy">"${kc.empathy_opener || ''}"</div>
        <div class="copy-label">솔루션 등장 문구</div>
        <div class="copy-block solution">"${kc.solution_reveal || ''}"</div>
        ${kc.problem_agitation?.length ? `<div class="copy-label">문제 심화 문구</div><ul>${kc.problem_agitation.map(t => `<li class="copy-agitation">${t}</li>`).join('')}</ul>` : ''}
        ${kc.core_benefits?.length ? `<div class="copy-label">핵심 베네핏</div><ul>${kc.core_benefits.map(t => `<li class="copy-benefit">✅ ${t}</li>`).join('')}</ul>` : ''}
      </div>`;

    // CTA
    if (kc.cta_options?.length) {
      body.innerHTML += `<div class="report-card"><h3>🛒 CTA 옵션</h3><div class="cta-grid">${kc.cta_options.map(c => `
        <div class="cta-item">
          <div class="cta-btn-preview">${c.text}</div>
          <div class="cta-urgency">${c.urgency_element || ''}</div>
          <div class="cta-reason">${c.reason || ''}</div>
        </div>`).join('')}</div></div>`;
    }
  }

  // 페이지 섹션 흐름
  if (r.page_sections?.length) {
    const sections = r.page_sections.map(s => `
      <div class="page-section-item">
        <div class="section-order">${s.order}</div>
        <div class="section-body">
          <div class="section-name">${s.section_name} <span class="section-technique">${s.hook_technique || ''}</span></div>
          <div class="section-headline">"${s.headline}"</div>
          <div class="section-copy">${s.body_copy}</div>
          <div class="section-visual">📸 ${s.visual_suggestion}</div>
        </div>
      </div>`).join('');
    body.innerHTML += `<div class="report-card"><h3>📋 페이지 섹션 흐름</h3><div class="page-sections-list">${sections}</div></div>`;
  }

  // 신뢰 구축
  if (r.trust_building) {
    const tb = r.trust_building;
    body.innerHTML += `
      <div class="report-grid-2">
        <div class="report-card">
          <h3>⭐ 신뢰 구축 전략</h3>
          ${tb.before_after ? `<p><strong>Before/After:</strong> ${tb.before_after}</p>` : ''}
          ${tb.review_keywords?.length ? `<div class="copy-label">강조할 리뷰 키워드</div><div class="tag-list">${tb.review_keywords.map(k => `<span class="tag">${k}</span>`).join('')}</div>` : ''}
          ${tb.certification_suggestions?.length ? `<div class="copy-label">인증·보증 요소</div><ul>${tb.certification_suggestions.map(t => `<li>${t}</li>`).join('')}</ul>` : ''}
        </div>
        <div class="report-card">
          <h3>🏆 차별화 포인트</h3>
          <ul>${(r.differentiation || []).map(t => `<li>${t}</li>`).join('')}</ul>
        </div>
      </div>`;
  }

  // 추천 타이틀
  if (r.recommended_titles?.length) {
    body.innerHTML += `<div class="report-card"><h3>📌 추천 상세페이지 타이틀</h3>${r.recommended_titles.map(t => `
      <div class="title-rec">
        <div class="title-text">${t.title}</div>
        <div class="title-reason">${t.hook_reason}</div>
      </div>`).join('')}</div>`;
  }

  document.getElementById('detail-report-section').classList.remove('hidden');
  document.getElementById('detail-report-section').scrollIntoView({ behavior: 'smooth' });
}

// ===== ✏️ 편집 피드백 =====

function startEditAnalysis() {
  if (editAnalyzing) return;
  const keyword = document.getElementById('edit-keyword-input').value.trim();
  const script = document.getElementById('edit-script-input').value.trim();
  if (!keyword) { document.getElementById('edit-keyword-input').focus(); return; }
  if (!script) { document.getElementById('edit-script-input').focus(); return; }
  const product_url = (document.getElementById('edit-product-url')?.value || '').trim();
  runEditAnalysis(keyword, script, product_url);
}

async function runEditAnalysis(keyword, script, product_url = '') {
  editAnalyzing = true;
  document.getElementById('edit-analyze-btn').disabled = true;
  document.getElementById('edit-input-section').classList.add('hidden');
  document.getElementById('edit-report-section').classList.add('hidden');
  document.getElementById('edit-progress-steps').innerHTML = '';
  document.getElementById('edit-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('edit-progress-steps');
  addStep('분석 준비 중...', 'active');

  await streamSSE(
    '/api/edit-feedback', { keyword, script, product_url },
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

  // ViewTrap 레퍼런스 섹션
  const vtCard = document.getElementById('edit-viewtrap-card');
  const vti = r.viewtrap_insights;
  const vtTop = r.viewtrap_top || [];
  const vtHot = r.viewtrap_hot || [];
  const hasVt = (vti && (vti.applied_patterns || (vti.referenced_videos && vti.referenced_videos.length))) || vtTop.length || vtHot.length;
  if (vtCard) {
    if (hasVt) {
      vtCard.style.display = '';
      const vtInsightsEl = document.getElementById('edit-viewtrap-insights');
      const vtTabsEl = document.getElementById('edit-viewtrap-tabs');
      const vtVideosEl = document.getElementById('edit-viewtrap-videos');

      let insHtml = '';
      if (vti && vti.applied_patterns) insHtml += `<div class="vt-applied">${vti.applied_patterns}</div>`;
      if (vti && vti.referenced_videos && vti.referenced_videos.length) {
        insHtml += `<div class="vt-refs-label">AI가 참고한 레퍼런스 영상</div><ul class="vt-refs-list">`;
        vti.referenced_videos.forEach(t => { insHtml += `<li>${t}</li>`; });
        insHtml += '</ul>';
      }
      if (vtInsightsEl) vtInsightsEl.innerHTML = insHtml;

      function renderVtVideosEdit(list) {
        if (!vtVideosEl) return;
        vtVideosEl.innerHTML = '';
        list.forEach(v => {
          const card = document.createElement('a');
          card.href = v.url || '#';
          card.target = '_blank';
          card.rel = 'noopener';
          card.className = 'vt-video-card';
          const perf = v.performance_rate_str || '';
          const perfClass = perf === '매우 좋음' || perf === '좋음' ? 'perf-good' : perf === '보통' ? 'perf-avg' : 'perf-bad';
          card.innerHTML = `
            <img src="${v.thumbnail || ''}" alt="" loading="lazy" class="vt-thumb" />
            <div class="vt-info">
              <div class="vt-title">${v.title || ''}</div>
              <div class="vt-meta"><span class="vt-channel">${v.channel || ''}</span><span class="vt-perf ${perfClass}">${perf}</span></div>
            </div>`;
          vtVideosEl.appendChild(card);
        });
      }

      if (vtTabsEl) {
        vtTabsEl.innerHTML = '';
        if (vtTop.length) {
          const btn1 = document.createElement('button');
          btn1.className = 'vt-tab active';
          btn1.textContent = `성과 영상 ${vtTop.length}개`;
          btn1.onclick = () => { vtTabsEl.querySelectorAll('.vt-tab').forEach(b => b.classList.remove('active')); btn1.classList.add('active'); renderVtVideosEdit(vtTop); };
          vtTabsEl.appendChild(btn1);
        }
        if (vtHot.length) {
          const btn2 = document.createElement('button');
          btn2.className = 'vt-tab' + (vtTop.length ? '' : ' active');
          btn2.textContent = `핫비디오 ${vtHot.length}개`;
          btn2.onclick = () => { vtTabsEl.querySelectorAll('.vt-tab').forEach(b => b.classList.remove('active')); btn2.classList.add('active'); renderVtVideosEdit(vtHot); };
          vtTabsEl.appendChild(btn2);
        }
      }
      renderVtVideosEdit(vtTop.length ? vtTop : vtHot);
    } else {
      vtCard.style.display = 'none';
    }
  }

  // 편집 채팅 컨텍스트 초기화
  initEditChat(r, keyword);
}

function initEditChat(r, keyword) {
  editChatHistory = [];
  editChatAttachments = [];

  const keeps = (r.keep_sections || []).map(s => `• ${s.section}: ${s.reason}`).join('\n');
  const cuts = (r.cut_sections || []).map(s => `• ${s.section}: ${s.reason}${s.alternative ? ` (대안: ${s.alternative})` : ''}`).join('\n');
  const missing = (r.missing_content || []).join(', ');
  const titles = (r.recommended_titles || []).map((t, i) => `${i+1}. ${t.title || t}`).join('\n');

  const contextMsg = `[편집 피드백 분석 결과 — "${keyword}"]
시장 적합도: ${r.market_fit_score || 0}점
전반적 평가: ${r.overall_assessment || ''}
잘 된 점: ${(r.strengths || []).join(', ')}
살려야 할 구간:\n${keeps}
삭제 추천 구간:\n${cuts}
인트로 후킹 피드백: ${r.hook_feedback || ''}
편집 흐름 개선점: ${(r.edit_flow_suggestions || []).join(', ')}
시청자가 원하는데 빠진 내용: ${missing}
추천 제목:\n${titles}`;

  editChatHistory.push({ role: 'user', content: contextMsg });
  editChatHistory.push({ role: 'assistant', content: `네, "${keyword}" 편집 피드백을 확인했습니다. 시장 적합도 ${r.market_fit_score || 0}점이고, 살릴 구간과 수정 포인트가 정리됐어요. 어떤 부분부터 작업할지, 구체적으로 어떻게 편집할지 궁금한 게 있으면 뭐든지 질문해 주세요!` });

  const messagesEl = document.getElementById('edit-chat-messages');
  messagesEl.innerHTML = '';
  const welcome = document.createElement('div');
  welcome.className = 'chat-bubble assistant';
  welcome.innerHTML = `<div class="chat-bubble-inner">피드백 분석 완료! <strong>"${keyword}"</strong> 편집 방향에 대해 자유롭게 질문해 주세요.<br>어떤 구간부터 잘라야 할지, 훅을 어떻게 고쳐야 할지 등 구체적으로 도와드릴 수 있어요.</div>`;
  messagesEl.appendChild(welcome);
}

function sendEditChip(el) {
  document.getElementById('edit-chat-input').value = el.textContent;
  sendEditChat();
}

function editChatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendEditChat();
  }
}

function handleEditChatFiles(input) {
  const preview = document.getElementById('edit-chat-attach-preview');
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
    editChatAttachments.push(...results);
    preview.classList.remove('hidden');
    preview.innerHTML = editChatAttachments.map((a, i) => {
      if (a.media_type.startsWith('image/')) {
        return `<div class="attach-thumb-wrap"><img class="attach-thumb" src="data:${a.media_type};base64,${a.data}" title="${a.name}" /><button class="attach-remove" onclick="removeEditAttach(${i})">✕</button></div>`;
      }
      return `<div class="attach-thumb-wrap"><div class="attach-pdf-badge">📄 ${a.name}</div><button class="attach-remove" onclick="removeEditAttach(${i})">✕</button></div>`;
    }).join('');
  });
  input.value = '';
}

function removeEditAttach(idx) {
  editChatAttachments.splice(idx, 1);
  const preview = document.getElementById('edit-chat-attach-preview');
  if (!editChatAttachments.length) { preview.innerHTML = ''; preview.classList.add('hidden'); return; }
  preview.innerHTML = editChatAttachments.map((a, i) => {
    if (a.media_type.startsWith('image/')) {
      return `<div class="attach-thumb-wrap"><img class="attach-thumb" src="data:${a.media_type};base64,${a.data}" title="${a.name}" /><button class="attach-remove" onclick="removeEditAttach(${i})">✕</button></div>`;
    }
    return `<div class="attach-thumb-wrap"><div class="attach-pdf-badge">📄 ${a.name}</div><button class="attach-remove" onclick="removeEditAttach(${i})">✕</button></div>`;
  }).join('');
}

async function sendEditChat() {
  if (editChatSending) return;
  const input = document.getElementById('edit-chat-input');
  const message = input.value.trim();
  if (!message && !editChatAttachments.length) return;

  editChatSending = true;
  document.getElementById('edit-chat-send-btn').disabled = true;
  input.value = '';
  input.style.height = 'auto';

  const attachmentsToSend = [...editChatAttachments];
  editChatAttachments = [];
  const preview = document.getElementById('edit-chat-attach-preview');
  preview.innerHTML = '';
  preview.classList.add('hidden');

  const messages = document.getElementById('edit-chat-messages');

  const userBubble = document.createElement('div');
  userBubble.className = 'chat-bubble user';
  let thumbsHtml = attachmentsToSend.map(a => a.media_type.startsWith('image/')
    ? `<img class="attach-thumb sent" src="data:${a.media_type};base64,${a.data}" />`
    : `<div class="attach-pdf-badge sent">📄 ${a.name}</div>`
  ).join('');
  userBubble.innerHTML = `<div class="chat-bubble-inner">${thumbsHtml ? `<div class="bubble-attachments">${thumbsHtml}</div>` : ''}${message ? _escapeHtml(message) : ''}</div>`;
  messages.appendChild(userBubble);

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
      body: JSON.stringify({ message, history: editChatHistory, attachments: attachmentsToSend }),
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
            editChatHistory.push({ role: 'user', content: message });
            editChatHistory.push({ role: 'assistant', content: fullText });
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

  editChatSending = false;
  document.getElementById('edit-chat-send-btn').disabled = false;
  input.focus();
}

// ===== 📣 SNS 변환 =====

let snsAnalyzing = false;

function resetToSns() {
  document.getElementById('sns-report-section').classList.add('hidden');
  document.getElementById('sns-progress-section').classList.add('hidden');
  document.getElementById('sns-input-section').classList.remove('hidden');
  document.getElementById('sns-analyze-btn').disabled = false;
  snsAnalyzing = false;
}

function startSnsConvert() {
  if (snsAnalyzing) return;
  const keyword = document.getElementById('sns-keyword-input').value.trim();
  const script = document.getElementById('sns-script-input').value.trim();
  if (!keyword) { alert('키워드를 입력해주세요.'); return; }
  if (!script) { alert('대본 또는 내용을 입력해주세요.'); return; }
  runSnsConvert(keyword, script);
}

function runSnsConvert(keyword, script) {
  snsAnalyzing = true;
  document.getElementById('sns-analyze-btn').disabled = true;
  document.getElementById('sns-input-section').classList.add('hidden');
  document.getElementById('sns-progress-section').classList.remove('hidden');

  const addStep = makeProgressStepper('sns-progress-steps');

  streamSSE(
    '/api/sns-convert',
    { keyword, script },
    addStep,
    (data) => {
      addStep('변환 완료!', 'done');
      setTimeout(() => renderSnsReport(data.report, data.keyword), 400);
    },
    (msg) => {
      addStep(`오류: ${msg}`, 'error');
      document.getElementById('sns-analyze-btn').disabled = false;
      snsAnalyzing = false;
    }
  );
}

function renderSnsReport(r, keyword) {
  document.getElementById('sns-progress-section').classList.add('hidden');
  document.getElementById('sns-report-section').classList.remove('hidden');
  document.getElementById('sns-report-title').textContent = `"${keyword}" SNS 변환`;

  // 블로그
  const blog = r.blog || {};
  document.getElementById('sns-blog-title').textContent = blog.title || '';
  document.getElementById('sns-blog-meta').textContent = blog.meta_description || '';
  document.getElementById('sns-blog-keyword-note').textContent = blog.keyword_count_note || '';
  document.getElementById('sns-blog-content').textContent = blog.content || '';
  const tagsEl = document.getElementById('sns-blog-tags');
  tagsEl.innerHTML = (blog.seo_tags || []).map(t => `<span class="sns-tag">${t}</span>`).join('');

  // 스레드
  const threads = r.threads || {};
  const postsEl = document.getElementById('sns-threads-posts');
  postsEl.innerHTML = '';
  (threads.posts || []).forEach((p, i) => {
    const div = document.createElement('div');
    div.className = `sns-thread-post ${p.type === 'hook' ? 'thread-hook' : ''}`;
    div.innerHTML = `
      <div class="thread-post-num">${i + 1}</div>
      <div class="thread-post-content">${_escapeHtml(p.content)}</div>
      <button class="thread-copy-btn" onclick="copyText2('${encodeURIComponent(p.content)}')">복사</button>
    `;
    postsEl.appendChild(div);
  });

  // 숏폼
  const sf = r.shortform || {};
  document.getElementById('sns-shortform-hook-box').innerHTML = `
    <div class="sns-hook-label">훅 (0~3초) <span class="sns-hook-type">${sf.hook_type || ''}</span></div>
    <div class="sns-hook-text">${_escapeHtml(sf.hook || '')}</div>
  `;

  const bodyEl = document.getElementById('sns-shortform-body');
  bodyEl.innerHTML = '';
  (sf.body_points || []).forEach(bp => {
    const div = document.createElement('div');
    div.className = 'sns-body-point';
    div.innerHTML = `
      <div class="sns-body-time">${bp.time}</div>
      <div class="sns-body-narration">${_escapeHtml(bp.narration || '')}</div>
      <div class="sns-body-overlay">📺 ${_escapeHtml(bp.text_overlay || '')}</div>
    `;
    bodyEl.appendChild(div);
  });

  document.getElementById('sns-shortform-cta-box').innerHTML = `
    <div class="sns-cta-label">CTA (마지막 5초)</div>
    <div class="sns-cta-text">${_escapeHtml(sf.cta || '')}</div>
  `;

  const platforms = document.getElementById('sns-shortform-platforms');
  platforms.innerHTML = (sf.platforms || []).map(p => `<span class="sns-platform-badge">${p}</span>`).join('');

  // 복사용 전체 스크립트 조합
  const fullScript = [
    `[훅] ${sf.hook || ''}`,
    ...(sf.body_points || []).map(bp => `[${bp.time}]\n${bp.narration}\n자막: ${bp.text_overlay}`),
    `[CTA] ${sf.cta || ''}`,
  ].join('\n\n');
  document.getElementById('sns-shortform-full').textContent = fullScript;
}

function copyText2(encoded) {
  const text = decodeURIComponent(encoded);
  navigator.clipboard.writeText(text).then(() => {
    // brief visual feedback handled by button styling
  });
}

function copySnsText(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent || el.value || '');
}

function copyAllThreads() {
  const posts = document.querySelectorAll('#sns-threads-posts .thread-post-content');
  const text = Array.from(posts).map((p, i) => `${i + 1}.\n${p.textContent}`).join('\n\n---\n\n');
  navigator.clipboard.writeText(text);
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
    sns: 'SNS 변환', research: '시장조사', planning: '기획', intro: '도입부', script: '대본',
    video_feedback: '🎬 영상 피드백'
  };
  const typeColors = {
    topic: '#ef4444', midform: '#3b82f6', shortform: '#ec4899', edit: '#8b5cf6',
    sns: '#f97316', research: '#6366f1', planning: '#f59e0b', intro: '#10b981', script: '#ef4444',
    video_feedback: '#0ea5e9'
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
    video_feedback: () => {
      switchTab('video-feedback');
      setTimeout(() => {
        const resultEl = document.getElementById('vf-result');
        renderVideoFeedback(data.report, resultEl);
        resultEl.classList.remove('hidden');
        document.getElementById('vf-progress').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    sns: () => {
      switchTab('sns'); resetToSns();
      setTimeout(() => {
        renderSnsReport(data.report, data.keyword);
        document.getElementById('sns-report-section').classList.remove('hidden');
        document.getElementById('sns-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    blog: () => {
      switchTab('blog'); resetBlog();
      setTimeout(() => {
        renderBlogResult(data.report, data.keyword);
        document.getElementById('blog-report-section').classList.remove('hidden');
        document.getElementById('blog-input-section').classList.add('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    channel: () => {
      switchTab('channel'); resetToChannel();
      setTimeout(() => {
        renderChannelReport(data.report);
        document.getElementById('channel-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    decision: () => {
      switchTab('decision'); resetToDecision();
      setTimeout(() => {
        renderDecisionReport(data.report);
        document.getElementById('decision-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }, 100);
    },
    detail_page: () => {
      switchTab('detail');
      setTimeout(() => {
        renderDetailPageReport(data.report, data.keyword);
        document.getElementById('detail-report-section').classList.remove('hidden');
        document.getElementById('detail-input-section').classList.add('hidden');
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

  // Most Replayed 히트맵 인사이트
  const hi = r.heatmap_insights;
  const heatmapCard = document.getElementById('heatmap-card');
  if (heatmapCard) {
    if (hi && hi.available) {
      heatmapCard.style.display = '';
      document.getElementById('heatmap-pattern').textContent = hi.pattern_summary || '';
      const hmList = document.getElementById('heatmap-moments');
      hmList.innerHTML = (hi.hot_moments || []).map(m => `<li>${m}</li>`).join('');
      const etList = document.getElementById('heatmap-tips');
      etList.innerHTML = (hi.editor_tips || []).map(t => `<li>${t}</li>`).join('');
    } else {
      heatmapCard.style.display = 'none';
    }
  }
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
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
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

// ===== 📝 블로그 기획 =====

let blogPhotos = [];  // [{name, media_type, data}]

function handleBlogPhotos(input) {
  const files = [...input.files];
  const countEl = document.getElementById('blog-photo-count');
  const preview = document.getElementById('blog-photo-preview');

  const readFile = (file) => new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const b64 = e.target.result.split(',')[1];
      const media_type = e.target.result.match(/:(.*?);/)[1];
      resolve({ name: file.name, media_type, data: b64 });
    };
    reader.readAsDataURL(file);
  });

  Promise.all(files.map(readFile)).then(results => {
    blogPhotos.push(...results);
    _renderBlogPhotoPreview();
    countEl.textContent = `${blogPhotos.length}장 선택됨`;
  });
  input.value = '';
}

function _renderBlogPhotoPreview() {
  const preview = document.getElementById('blog-photo-preview');
  if (!blogPhotos.length) {
    preview.classList.add('hidden');
    preview.innerHTML = '';
    return;
  }
  preview.classList.remove('hidden');
  preview.innerHTML = blogPhotos.map((a, i) => `
    <div class="blog-photo-thumb-wrap">
      <span class="blog-photo-num">${i + 1}</span>
      <img class="blog-photo-thumb" src="data:${a.media_type};base64,${a.data}" title="${a.name}"/>
      <button class="blog-photo-remove" onclick="removeBlogPhoto(${i})">✕</button>
      <div class="blog-photo-name">${a.name}</div>
    </div>`).join('');
}

function removeBlogPhoto(idx) {
  blogPhotos.splice(idx, 1);
  const countEl = document.getElementById('blog-photo-count');
  countEl.textContent = blogPhotos.length ? `${blogPhotos.length}장 선택됨` : '';
  _renderBlogPhotoPreview();
}

function resetBlog() {
  document.getElementById('blog-input-section').classList.remove('hidden');
  document.getElementById('blog-progress-section').classList.add('hidden');
  document.getElementById('blog-report-section').classList.add('hidden');
  document.getElementById('blog-progress-steps').innerHTML = '';
  document.getElementById('blog-btn').disabled = false;
  document.getElementById('blog-btn').textContent = '블로그 초안 생성';
  blogPhotos = [];
  document.getElementById('blog-photo-preview').innerHTML = '';
  document.getElementById('blog-photo-preview').classList.add('hidden');
  document.getElementById('blog-photo-count').textContent = '';
}

async function startBlog() {
  const keyword = document.getElementById('blog-keyword').value.trim();
  const memo    = document.getElementById('blog-memo').value.trim();
  const region  = (document.getElementById('blog-region')?.value || '').trim();
  const link    = (document.getElementById('blog-link')?.value || '').trim();

  if (!keyword) { alert('키워드/제목을 입력해주세요.'); return; }

  const btn = document.getElementById('blog-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 생성 중...';

  document.getElementById('blog-input-section').classList.add('hidden');
  document.getElementById('blog-progress-section').classList.remove('hidden');
  document.getElementById('blog-report-section').classList.add('hidden');
  document.getElementById('blog-progress-steps').innerHTML = '';

  const addStep = makeProgressStepper('blog-progress-steps');
  addStep('원고 준비 중...', 'active');

  // photos는 {media_type, data}만 전송 (name 제외)
  const photosPayload = blogPhotos.map(p => ({ media_type: p.media_type, data: p.data }));

  await streamSSE(
    '/api/blog',
    { keyword, memo, region, link, photos: photosPayload },
    addStep,
    (data) => {
      document.getElementById('blog-progress-steps').querySelectorAll('.progress-step.active').forEach(s => {
        s.className = 'progress-step done';
        s.querySelector('.step-icon').textContent = '✅';
      });
      addStep('원고 완성!', 'done');
      setTimeout(() => {
        document.getElementById('blog-progress-section').classList.add('hidden');
        renderBlogResult(data.result, data.keyword || keyword);
        document.getElementById('blog-report-section').classList.remove('hidden');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        btn.disabled = false;
        btn.textContent = '블로그 초안 생성';
      }, 600);
    },
    (msg) => {
      document.getElementById('blog-progress-steps').innerHTML = '';
      makeProgressStepper('blog-progress-steps')(msg, 'error');
      document.getElementById('blog-input-section').classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = '블로그 초안 생성';
    }
  );
}

// 마크다운 표 → HTML 테이블 변환
function _mdTableToHtml(text) {
  const lines = text.split('\n');
  let inTable = false;
  let html = '';
  let tableLines = [];

  const flushTable = () => {
    if (!tableLines.length) return;
    html += '<table class="blog-md-table">';
    tableLines.forEach((row, i) => {
      if (row.replace(/[\s|:-]/g, '') === '') return; // separator row
      const cells = row.split('|').map(c => c.trim()).filter((c, ci, arr) => ci > 0 && ci < arr.length - 1);
      const tag = i === 0 ? 'th' : 'td';
      html += '<tr>' + cells.map(c => `<${tag}>${c}</${tag}>`).join('') + '</tr>';
    });
    html += '</table>';
    tableLines = [];
    inTable = false;
  };

  lines.forEach(line => {
    if (line.trim().startsWith('|')) {
      inTable = true;
      tableLines.push(line);
    } else {
      if (inTable) flushTable();
      if (line.trim()) html += `<p>${line}</p>`;
    }
  });
  if (inTable) flushTable();
  return html || `<p>${text}</p>`;
}

function renderBlogResult(r, keyword) {
  document.getElementById('blog-report-title').textContent = `"${keyword}" 블로그 원고`;

  // ── 1. 제목 후보 3개 ──
  const titlesEl = document.getElementById('blog-titles');
  titlesEl.innerHTML = '';
  (r.titles || []).forEach((title, i) => {
    const card = document.createElement('div');
    card.className = 'blog-title-card';
    card.title = '클릭하면 복사됩니다';
    card.innerHTML = `<span class="blog-title-num">제목 ${i + 1}</span><span class="blog-title-text">${title}</span>`;
    card.addEventListener('click', () => {
      navigator.clipboard.writeText(title).then(() => {
        const orig = card.innerHTML;
        card.innerHTML = `<span style="color:#059669;font-weight:700;font-size:15px">✅ 복사됨!</span>`;
        setTimeout(() => { card.innerHTML = orig; }, 1500);
      });
    });
    titlesEl.appendChild(card);
  });

  // ── 2. 본문 섹션 ──
  const bodySections = document.getElementById('blog-body-sections');
  bodySections.innerHTML = '';
  (r.sections || []).forEach(sec => {
    const secEl = document.createElement('div');
    secEl.className = 'blog-section';

    const headingEl = document.createElement('h2');
    headingEl.className = 'blog-section-heading';
    headingEl.textContent = sec.heading || '';
    secEl.appendChild(headingEl);

    const isSubtitle2 = (sec.heading || '').includes('소제목2') || (sec.heading || '').includes('소제목 2');

    // 새 구조: body + photo_captions
    if (sec.body !== undefined || sec.photo_captions !== undefined) {
      // 본문 body
      if (sec.body) {
        const bodyEl = document.createElement('div');
        bodyEl.className = 'blog-para-text blog-section-body';
        if (isSubtitle2 && sec.body.includes('|')) {
          bodyEl.innerHTML = _mdTableToHtml(sec.body);
        } else {
          bodyEl.textContent = sec.body;
        }
        secEl.appendChild(bodyEl);
      }
      // 사진 한 줄 캡션
      (sec.photo_captions || []).forEach(pc => {
        const captionWrap = document.createElement('div');
        captionWrap.className = 'blog-para-wrap blog-caption-wrap';
        const photoData = blogPhotos[pc.photo_index];
        if (photoData) {
          const thumbWrap = document.createElement('div');
          thumbWrap.className = 'blog-inline-thumb-wrap';
          const badge = document.createElement('span');
          badge.className = 'blog-photo-badge';
          badge.textContent = `사진 ${pc.photo_index + 1}`;
          const thumb = document.createElement('img');
          thumb.className = 'blog-inline-thumb';
          thumb.src = `data:${photoData.media_type};base64,${photoData.data}`;
          thumb.alt = photoData.name;
          thumbWrap.appendChild(badge);
          thumbWrap.appendChild(thumb);
          captionWrap.appendChild(thumbWrap);
        } else {
          const badge = document.createElement('span');
          badge.className = 'blog-photo-badge';
          badge.textContent = `사진 ${pc.photo_index + 1}`;
          captionWrap.appendChild(badge);
        }
        const captionEl = document.createElement('div');
        captionEl.className = 'blog-para-text blog-photo-caption';
        captionEl.textContent = pc.caption || '';
        captionWrap.appendChild(captionEl);
        secEl.appendChild(captionWrap);
      });
    } else {
      // 구 구조 호환: paragraphs
      (sec.paragraphs || []).forEach(para => {
        const paraWrap = document.createElement('div');
        paraWrap.className = 'blog-para-wrap';
        if (typeof para.photo_index === 'number') {
          const badge = document.createElement('span');
          badge.className = 'blog-photo-badge';
          badge.textContent = `사진 ${para.photo_index + 1}`;
          const photoData = blogPhotos[para.photo_index];
          if (photoData) {
            const thumbWrap = document.createElement('div');
            thumbWrap.className = 'blog-inline-thumb-wrap';
            const thumb = document.createElement('img');
            thumb.className = 'blog-inline-thumb';
            thumb.src = `data:${photoData.media_type};base64,${photoData.data}`;
            thumb.alt = photoData.name;
            thumbWrap.appendChild(badge.cloneNode(true));
            thumbWrap.appendChild(thumb);
            paraWrap.appendChild(thumbWrap);
          } else {
            paraWrap.appendChild(badge);
          }
        }
        const textEl = document.createElement('div');
        textEl.className = 'blog-para-text';
        if (isSubtitle2 && (para.text || '').includes('|')) {
          textEl.innerHTML = _mdTableToHtml(para.text || '');
        } else {
          textEl.textContent = para.text || '';
        }
        paraWrap.appendChild(textEl);
        secEl.appendChild(paraWrap);
      });
    }

    bodySections.appendChild(secEl);
  });

  // ── 3. 사진 파일명 ──
  const filenamesEl = document.getElementById('blog-filenames');
  const filenamesCard = document.getElementById('blog-filenames-card');
  const filenames = r.filenames || [];
  if (filenames.length) {
    filenamesCard.classList.remove('hidden');
    filenamesEl.innerHTML = '<ol class="blog-filename-list">' +
      filenames.map((name, i) => `
        <li class="blog-filename-item" onclick="copyBlogFilename(this, '${name.replace(/'/g, "\\'")}')" title="클릭하면 복사">
          <span class="blog-filename-num">${i + 1}</span>
          <span class="blog-filename-text">${name}</span>
          <span class="blog-filename-copy-hint">복사</span>
        </li>`).join('') +
      '</ol>';
  } else {
    filenamesCard.classList.add('hidden');
  }

  // ── 4. 태그 30개 ──
  const tagsEl = document.getElementById('blog-tags');
  tagsEl.innerHTML = '<div class="blog-tags-wrap">' +
    (r.tags || []).map(tag => `<span class="blog-tag-chip">${tag}</span>`).join('') +
    '</div>';

  // ── 5. 발행 가이드 ──
  const guide = (r.checklist || {}).publish_guide || {};
  const pubEl = document.getElementById('blog-publish-guide');
  pubEl.innerHTML = `
    <div class="blog-guide-grid">
      <div class="blog-guide-item">
        <div class="blog-guide-label">권장 발행 시간</div>
        <div class="blog-guide-value">${guide.best_time || '오전 7~9시 또는 오후 12~1시'}</div>
      </div>
      <div class="blog-guide-item">
        <div class="blog-guide-label">발행 주기</div>
        <div class="blog-guide-value">${guide.frequency || '주 2~3회 꾸준히'}</div>
      </div>
      <div class="blog-guide-item">
        <div class="blog-guide-label">같은 주제 간격</div>
        <div class="blog-guide-value">${guide.interval || '같은 주제는 2주 간격'}</div>
      </div>
      <div class="blog-guide-item">
        <div class="blog-guide-label">최적화 팁</div>
        <div class="blog-guide-value">${guide.optimization || '발행 후 2~3일 내 반응 보고 제목·첫 문단 수정 가능'}</div>
      </div>
    </div>`;

  // ── 6. 저품질 방지 체크리스트 ──
  const quality = (r.checklist || {}).quality || [
    '복붙 금지',
    '외부 링크 3개 초과 금지',
    '키워드 나열 금지',
    '하루 1개 이상 포스팅 금지',
    '다른 블로그 사진 무단 사용 금지',
  ];
  const checklistEl = document.getElementById('blog-checklist');
  checklistEl.innerHTML = '<div class="blog-checklist-wrap">' +
    quality.map((item, i) => `
      <label class="blog-checklist-item">
        <input type="checkbox" id="blog-check-${i}" class="blog-check-input"/>
        <span class="blog-check-label">${item}</span>
      </label>`).join('') +
    '</div>';
}

function copyBlogFilename(el, name) {
  navigator.clipboard.writeText(name).then(() => {
    const hint = el.querySelector('.blog-filename-copy-hint');
    if (hint) { hint.textContent = '✅'; setTimeout(() => { hint.textContent = '복사'; }, 1500); }
  });
}

function copyBlogBody() {
  const sections = document.getElementById('blog-body-sections');
  // 텍스트 내용만 수집
  let text = '';
  sections.querySelectorAll('.blog-section').forEach(sec => {
    const heading = sec.querySelector('.blog-section-heading');
    if (heading) text += '\n\n' + heading.textContent + '\n';
    sec.querySelectorAll('.blog-para-text').forEach(p => {
      text += '\n' + p.textContent;
    });
  });
  navigator.clipboard.writeText(text.trim()).then(() => {
    const btn = document.getElementById('blog-copy-body-btn');
    if (btn) { btn.textContent = '✅ 복사됨'; setTimeout(() => btn.textContent = '전체 복사', 1500); }
  });
}

// ── 콘텐츠 파이프라인 ───────────────────────────────────────────────

const PIPELINE_STAGES = [
  { key: 'planning',   label: '기획',        emoji: '📋', color: '#6366f1', bg: '#eef2ff' },
  { key: 'filming',    label: '촬영',        emoji: '📹', color: '#8b5cf6', bg: '#f5f3ff' },
  { key: 'sent',       label: '편집자 전달', emoji: '📤', color: '#f97316', bg: '#fff7ed' },
  { key: 'editing',    label: '편집 중',     emoji: '✂️', color: '#eab308', bg: '#fefce8' },
  { key: 'done',       label: '편집 완료',   emoji: '✅', color: '#22c55e', bg: '#f0fdf4' },
  { key: 'thumbnail',  label: '섬네일 제작', emoji: '🖼️', color: '#ec4899', bg: '#fdf2f8' },
  { key: 'uploaded',   label: '업로드',      emoji: '🎬', color: '#06b6d4', bg: '#ecfeff' },
  { key: 'sns',        label: '기타 SNS 배포', emoji: '📣', color: '#10b981', bg: '#f0fdf4' },
];

const TYPE_COLORS = {
  '미드폼': { bg: '#dbeafe', color: '#1e40af' },
  '숏폼':   { bg: '#fed7aa', color: '#c2410c' },
  '쇼츠':   { bg: '#fed7aa', color: '#c2410c' }, // 숏폼과 동일 처리 (레거시)
  '기타':   { bg: '#f3f4f6', color: '#374151' },
};

let plVideos = [];
let plGroupBy = null;   // null | 'stage' | 'type' | 'editor'
let plFilterVal = null; // 특정 값으로 필터
let plCollapsed = new Set(JSON.parse(localStorage.getItem('pl_collapsed') || '[]'));
let plGroupIdMap = {}; // 그룹키 → [id, ...] 맵 (renderKanban에서 채움)

function collapseAll() {
  plVideos.forEach(v => plCollapsed.add(v.id));
  localStorage.setItem('pl_collapsed', JSON.stringify([...plCollapsed]));
  renderKanban();
}
function expandAll() {
  plCollapsed.clear();
  localStorage.setItem('pl_collapsed', JSON.stringify([]));
  renderKanban();
}
function toggleGroupCollapse(key) {
  const ids = plGroupIdMap[key] || [];
  const allCollapsed = ids.length > 0 && ids.every(id => plCollapsed.has(id));
  ids.forEach(id => allCollapsed ? plCollapsed.delete(id) : plCollapsed.add(id));
  localStorage.setItem('pl_collapsed', JSON.stringify([...plCollapsed]));
  renderKanban();
}
let plShowCalendar = JSON.parse(localStorage.getItem('pl_show_calendar') || 'true');
let plCalYear  = new Date().getFullYear();
let plCalMonth = new Date().getMonth();

function toggleCalendar() {
  plShowCalendar = !plShowCalendar;
  localStorage.setItem('pl_show_calendar', JSON.stringify(plShowCalendar));
  applyCalendarLayout();
  if (plShowCalendar) renderCalendar();
}

function applyCalendarLayout() {
  const panel = document.getElementById('pl-calendar-panel');
  const btn   = document.getElementById('pl-cal-toggle-btn');
  if (!panel) return;
  if (plShowCalendar) {
    panel.style.display = 'flex';
    btn && btn.classList.add('active');
  } else {
    panel.style.display = 'none';
    btn && btn.classList.remove('active');
  }
}

function changeCalendarMonth(dir) {
  plCalMonth += dir;
  if (plCalMonth > 11) { plCalMonth = 0; plCalYear++; }
  if (plCalMonth <  0) { plCalMonth = 11; plCalYear--; }
  renderCalendar();
}

function renderCalendar() {
  const gridEl  = document.getElementById('pl-cal-grid');
  const titleEl = document.getElementById('pl-cal-month-title');
  if (!gridEl || !plShowCalendar) return;

  const y = plCalYear, m = plCalMonth;
  const KR_MONTHS = ['1월','2월','3월','4월','5월','6월','7월','8월','9월','10월','11월','12월'];
  titleEl.textContent = `${y}년 ${KR_MONTHS[m]}`;

  // planned_date → videos 맵
  const dateMap = {};
  plVideos.forEach(v => {
    if (!v.planned_date) return;
    if (!dateMap[v.planned_date]) dateMap[v.planned_date] = [];
    dateMap[v.planned_date].push(v);
  });

  const firstDow  = new Date(y, m, 1).getDay();
  const totalDays = new Date(y, m + 1, 0).getDate();
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;

  let cells = Array(firstDow).fill(null);
  for (let d = 1; d <= totalDays; d++) cells.push(d);
  while (cells.length % 7) cells.push(null);

  let html = '';
  for (let i = 0; i < cells.length; i += 7) {
    html += '<div class="pl-cal-row">';
    for (let j = 0; j < 7; j++) {
      const d = cells[i + j];
      if (!d) { html += '<div class="pl-cal-cell empty"></div>'; continue; }
      const ds = `${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
      const vids = dateMap[ds] || [];
      const isToday = ds === todayStr;
      const isSun = j === 0, isSat = j === 6;
      html += `<div class="pl-cal-cell${isToday?' today':''}${isSun?' sun':isSat?' sat':''}">
        <div class="pl-cal-day-num">${d}</div>
        ${vids.map(v => {
          const tc = TYPE_COLORS[v.content_type] || TYPE_COLORS['기타'];
          const short = v.title.length > 7 ? v.title.slice(0,7)+'…' : v.title;
          return `<div class="pl-cal-event" style="background:${tc.bg};color:${tc.color};border-left:2px solid ${tc.color}" title="${v.title}">${short}</div>`;
        }).join('')}
      </div>`;
    }
    html += '</div>';
  }
  gridEl.innerHTML = html;
}

function toggleCollapse(id) {
  event.stopPropagation();
  if (plCollapsed.has(id)) plCollapsed.delete(id);
  else plCollapsed.add(id);
  localStorage.setItem('pl_collapsed', JSON.stringify([...plCollapsed]));
  renderKanban();
}

async function loadPipeline() {
  const res = await fetch('/api/pipeline');
  plVideos = await res.json();
  renderPipelineSummary();
  renderGroupControls();
  renderKanban();
  applyCalendarLayout();
  renderCalendar();
}

function plGroupField() {
  return plGroupBy === 'stage' ? 'stage' : plGroupBy === 'type' ? 'content_type' : 'editor';
}

function setGroupBy(key) {
  plGroupBy = plGroupBy === key ? null : key;
  plFilterVal = null;
  renderGroupControls();
  renderKanban();
}

function setPlFilter(val) {
  plFilterVal = plFilterVal === val ? null : val;
  renderGroupControls();
  renderKanban();
}

function resetPlControls() {
  plGroupBy = null; plFilterVal = null;
  renderGroupControls(); renderKanban();
}

function renderGroupControls() {
  const el = document.getElementById('pl-controls');
  if (!el) return;

  const groupBtns = [
    { key: 'stage',  label: '단계별' },
    { key: 'type',   label: '유형별' },
    { key: 'editor', label: '편집자별' },
  ].map(g => `
    <button class="pl-group-btn${plGroupBy === g.key ? ' active' : ''}" onclick="setGroupBy('${g.key}')">${g.label}</button>
  `).join('');

  let filterRow = '';
  if (plGroupBy) {
    const field = plGroupField();
    const stageOrder = PIPELINE_STAGES.map(s => s.key);
    let vals = [...new Set(plVideos.map(v => v[field] || '미지정'))];
    if (plGroupBy === 'stage') vals.sort((a, b) => stageOrder.indexOf(a) - stageOrder.indexOf(b));
    const chips = vals.map(val => {
      const stage = plGroupBy === 'stage' ? PIPELINE_STAGES.find(s => s.key === val) : null;
      const label = stage ? `${stage.emoji} ${stage.label}` : val || '미지정';
      const color = stage ? stage.color : '#374151';
      const bg    = stage ? stage.bg    : '#f3f4f6';
      const active = plFilterVal === val;
      return `<button class="pl-filter-chip${active ? ' active' : ''}"
        style="${active ? `background:${color};color:#fff;border-color:${color}` : `background:${bg};color:${color};border-color:${color}40`}"
        onclick="setPlFilter('${val}')">${label}</button>`;
    }).join('');
    filterRow = `<div class="pl-filter-row"><span class="pl-ctrl-label">필터</span>${chips}</div>`;
  }

  const hasControl = plGroupBy || plFilterVal;
  el.innerHTML = `
    <div class="pl-ctrl-row">
      <span class="pl-ctrl-label">묶기</span>
      ${groupBtns}
      <div class="pl-ctrl-sep"></div>
      <button class="pl-fold-btn" onclick="collapseAll()" title="전체 접기">▶ 모두 접기</button>
      <button class="pl-fold-btn" onclick="expandAll()" title="전체 펼치기">▼ 모두 펼치기</button>
      ${hasControl ? `<button class="pl-ctrl-reset" onclick="resetPlControls()">× 초기화</button>` : ''}
    </div>
    ${filterRow}
  `;
}

function renderPipelineSummary() {
  const counts = {};
  PIPELINE_STAGES.forEach(s => counts[s.key] = 0);
  plVideos.forEach(v => { if (counts[v.stage] !== undefined) counts[v.stage]++; });
  document.getElementById('pl-stage-summary').innerHTML = PIPELINE_STAGES.map(s => `
    <div class="pl-sum-chip" style="background:${s.bg};border-color:${s.color}20;color:${s.color}">
      <span>${s.emoji}</span>
      <span class="pl-sum-label">${s.label}</span>
      <span class="pl-sum-count" style="background:${s.color};color:#fff">${counts[s.key]}</span>
    </div>
  `).join('');
}

function renderKanban() {
  const el = document.getElementById('pl-kanban');
  if (!plVideos.length) {
    el.innerHTML = '<div class="pl-empty">아직 추가된 영상이 없습니다.<br>우상단 <strong>+ 영상 추가</strong>를 눌러 시작하세요.</div>';
    return;
  }

  // 필터 적용
  const field = plGroupField();
  const videos = plFilterVal
    ? plVideos.filter(v => (v[field] || '미지정') === plFilterVal)
    : plVideos;

  if (!videos.length) {
    el.innerHTML = '<div class="pl-empty">조건에 맞는 영상이 없습니다.</div>';
    return;
  }

  // 그룹 없음
  if (!plGroupBy) {
    el.innerHTML = videos.map(v => plRow(v)).join('');
    return;
  }

  // 그룹별 묶기
  const stageOrder = PIPELINE_STAGES.map(s => s.key);
  const groups = {};
  videos.forEach(v => {
    const key = (v[field] || '미지정');
    if (!groups[key]) groups[key] = [];
    groups[key].push(v);
  });

  let sortedKeys = Object.keys(groups);
  if (plGroupBy === 'stage') {
    sortedKeys.sort((a, b) => stageOrder.indexOf(a) - stageOrder.indexOf(b));
  }

  // 그룹ID 맵 갱신
  plGroupIdMap = {};
  sortedKeys.forEach(key => { plGroupIdMap[key] = groups[key].map(v => v.id); });

  el.innerHTML = sortedKeys.map(key => {
    const stage = plGroupBy === 'stage' ? PIPELINE_STAGES.find(s => s.key === key) : null;
    const label = stage ? `${stage.emoji} ${stage.label}` : key;
    const color = stage ? stage.color : '#374151';
    const ids = groups[key].map(v => v.id);
    const count = ids.length;
    const allCollapsed = ids.length > 0 && ids.every(id => plCollapsed.has(id));
    return `
      <div class="pl-group">
        <div class="pl-group-header" style="color:${color};border-left-color:${color}" onclick="toggleGroupCollapse('${key}')">
          <span>${label}</span>
          <span class="pl-group-count" style="background:${color}">${count}</span>
          <span class="pl-group-fold">${allCollapsed ? '▶' : '▼'}</span>
        </div>
        <div class="pl-group-body">${groups[key].map(v => plRow(v)).join('')}</div>
      </div>`;
  }).join('');
}

function plRow(v) {
  const tc = TYPE_COLORS[v.content_type] || TYPE_COLORS['기타'];
  const curIdx = PIPELINE_STAGES.findIndex(s => s.key === v.stage);
  const cur = PIPELINE_STAGES[curIdx];
  const dateStr = v.planned_date ? `📅 ${v.planned_date}` : '';
  const editorStr = v.editor ? `✂️ ${v.editor}` : '';
  const collapsed = plCollapsed.has(v.id);

  const stepper = PIPELINE_STAGES.map((s, i) => {
    const done = i < curIdx;
    const active = i === curIdx;
    const dotStyle = active
      ? `background:${s.color};border-color:${s.color};color:#fff;box-shadow:0 0 0 4px ${s.color}40,0 4px 14px ${s.color}70`
      : done
        ? `background:${s.color}22;border-color:${s.color};color:${s.color}`
        : 'background:#f3f4f6;border-color:#d1d5db;color:#9ca3af';
    const labelStyle = active ? `color:${s.color};font-weight:800;font-size:11px` : done ? `color:${s.color}` : 'color:#9ca3af';
    const lineStyle = done || active ? `background:${PIPELINE_STAGES[i].color}` : 'background:#e5e7eb';
    const dot = done ? '✓' : s.emoji;
    return `
      <div class="pl-step-wrap">
        <button class="pl-step-dot${active ? ' pl-step-dot-active' : ''}" style="${dotStyle}" onclick="event.stopPropagation();setStage(${v.id},${i})" title="${s.label}로 이동">${dot}</button>
        <div class="pl-step-label" style="${labelStyle}">${s.label}</div>
      </div>
      ${i < PIPELINE_STAGES.length - 1 ? `<div class="pl-step-line" style="${lineStyle}"></div>` : ''}
    `;
  }).join('');

  return `
  <div class="pl-row${collapsed ? ' pl-row-collapsed' : ''}" onclick="openVideoModal(${v.id})">
    <div class="pl-row-head">
      <div class="pl-row-left">
        <span class="pl-type-badge" style="background:${tc.bg};color:${tc.color}">${v.content_type}</span>
        <span class="pl-row-title">${escHtml(v.title)}</span>
        ${!collapsed && (editorStr || dateStr) ? `<span class="pl-row-meta">${[editorStr, dateStr].filter(Boolean).join(' · ')}</span>` : ''}
      </div>
      <div class="pl-row-actions" onclick="event.stopPropagation()">
        <span class="pl-cur-stage" style="color:${cur.color};background:${cur.bg}">${cur.emoji} ${cur.label}</span>
        <button class="pl-arrow" onclick="moveStage(${v.id},-1)" ${curIdx>0?'':'disabled'}>←</button>
        <button class="pl-arrow" onclick="moveStage(${v.id},1)" ${curIdx<PIPELINE_STAGES.length-1?'':'disabled'}>→</button>
        <button class="pl-collapse-btn" onclick="toggleCollapse(${v.id})" title="${collapsed?'펼치기':'접기'}">${collapsed ? '▶' : '▼'}</button>
        <button class="pl-del" onclick="deleteVideo(${v.id})">🗑</button>
      </div>
    </div>
    ${!collapsed && v.notes ? `<div class="pl-row-notes">${escHtml(v.notes.slice(0,80))}${v.notes.length>80?'…':''}</div>` : ''}
    ${!collapsed ? `<div class="pl-stepper">${stepper}</div>` : ''}
  </div>`;
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function openVideoModal(id = null, defaultStage = 'planning') {
  document.getElementById('pl-modal-title').textContent = id ? '영상 수정' : '영상 추가';
  document.getElementById('pl-edit-id').value = id || '';
  if (id) {
    const v = plVideos.find(x => x.id === id);
    if (!v) return;
    document.getElementById('pl-f-title').value = v.title;
    document.getElementById('pl-f-type').value = v.content_type;
    document.getElementById('pl-f-stage').value = v.stage;
    document.getElementById('pl-f-editor').value = v.editor || '';
    document.getElementById('pl-f-date').value = v.planned_date || '';
    document.getElementById('pl-f-notes').value = v.notes || '';
  } else {
    document.getElementById('pl-f-title').value = '';
    document.getElementById('pl-f-type').value = '미드폼';
    document.getElementById('pl-f-stage').value = defaultStage;
    document.getElementById('pl-f-editor').value = '';
    document.getElementById('pl-f-date').value = '';
    document.getElementById('pl-f-notes').value = '';
  }
  document.getElementById('pl-modal').classList.remove('hidden');
  document.getElementById('pl-modal-overlay').classList.remove('hidden');
  document.getElementById('pl-f-title').focus();
}

function closeVideoModal() {
  document.getElementById('pl-modal').classList.add('hidden');
  document.getElementById('pl-modal-overlay').classList.add('hidden');
}

async function saveVideo() {
  const title = document.getElementById('pl-f-title').value.trim();
  if (!title) { alert('영상 제목을 입력해주세요.'); return; }
  const payload = {
    title,
    content_type: document.getElementById('pl-f-type').value,
    stage: document.getElementById('pl-f-stage').value,
    editor: document.getElementById('pl-f-editor').value.trim(),
    planned_date: document.getElementById('pl-f-date').value,
    notes: document.getElementById('pl-f-notes').value.trim(),
  };
  const editId = document.getElementById('pl-edit-id').value;
  if (editId) {
    await fetch(`/api/pipeline/${editId}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  } else {
    await fetch('/api/pipeline', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  }
  closeVideoModal();
  loadPipeline();
}

async function moveStage(id, dir) {
  const v = plVideos.find(x => x.id === id);
  if (!v) return;
  const idx = PIPELINE_STAGES.findIndex(s => s.key === v.stage);
  await setStage(id, idx + dir);
}

async function setStage(id, idx) {
  if (idx < 0 || idx >= PIPELINE_STAGES.length) return;
  await fetch(`/api/pipeline/${id}`, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ stage: PIPELINE_STAGES[idx].key })
  });
  loadPipeline();
}

async function deleteVideo(id) {
  if (!confirm('이 영상을 파이프라인에서 삭제하시겠습니까?')) return;
  await fetch(`/api/pipeline/${id}`, { method: 'DELETE' });
  loadPipeline();
}

// ===== 🎬 영상 피드백 =====

let vfSelectedFile = null;

function onVideoFileSelected(event) {
  const file = event.target.files[0];
  if (!file) return;
  vfSelectedFile = file;
  const sizeGB = (file.size / 1024 / 1024 / 1024).toFixed(2);
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  const sizeLabel = file.size >= 1024 * 1024 * 1024 ? `${sizeGB} GB` : `${sizeMB} MB`;
  document.getElementById('vf-file-name').textContent = `${file.name} (${sizeLabel})`;
  document.getElementById('vf-analyze-btn').disabled = false;
}

const VF_STEP_PROGRESS = {
  uploading: 10,
  validating: 15,
  extracting: 30,
  transcribing: 60,
  analyzing: 85,
  done: 100,
};

const VF_STEP_LABEL = {
  uploading: '영상 파일 저장 중...',
  validating: '파일 유효성 검사 중...',
  extracting: '오디오 추출 중...',
  transcribing: '자막 추출 중... (영상 길이에 따라 2~5분 소요)',
  analyzing: 'AI 피드백 분석 중...',
  done: '완료!',
  error: '오류 발생',
};

async function analyzeVideo() {
  if (!vfSelectedFile) return;

  const analyzeBtn = document.getElementById('vf-analyze-btn');
  analyzeBtn.disabled = true;
  analyzeBtn.textContent = '분석 중...';

  const progressEl = document.getElementById('vf-progress');
  const stepMsgEl = document.getElementById('vf-step-msg');
  const fillEl = document.getElementById('vf-progress-fill');
  const resultEl = document.getElementById('vf-result');

  progressEl.classList.remove('hidden');
  resultEl.classList.add('hidden');
  resultEl.innerHTML = '';

  function setProgress(step, msg) {
    const pct = VF_STEP_PROGRESS[step] || 0;
    stepMsgEl.textContent = msg || VF_STEP_LABEL[step] || step;
    fillEl.style.width = pct + '%';
  }

  // XHR로 업로드 진행률 표시 + SSE 스트림 수신
  const formData = new FormData();
  formData.append('file', vfSelectedFile);

  const fileSizeMB = (vfSelectedFile.size / 1024 / 1024).toFixed(0);

  await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/video-feedback');

    let resolved = false;
    function done() { if (!resolved) { resolved = true; clearTimeout(stallTimer); resolve(); } }

    // 무응답 감지: 마지막 SSE 수신 후 5분간 새 데이터 없으면 에러
    // (서버가 ffmpeg/Whisper ping을 5초마다 보내므로 실제로는 훨씬 빨리 감지됨)
    const STALL_MS = 5 * 60 * 1000;
    function onStall() {
      if (resolved) return;
      xhr.abort();
      setProgress('error', '응답이 없습니다. 파일이 손상되었거나 서버가 중단되었을 수 있습니다. 다시 시도해주세요.');
      fillEl.style.background = '#ef4444';
      analyzeBtn.disabled = false;
      analyzeBtn.textContent = 'AI 피드백 받기';
      done();
    }
    let stallTimer = setTimeout(onStall, STALL_MS);

    function resetStallTimer() { clearTimeout(stallTimer); stallTimer = setTimeout(onStall, STALL_MS); }

    // 업로드 진행률
    stepMsgEl.textContent = '서버로 전송 중...';
    fillEl.style.width = '2%';
    xhr.upload.onprogress = (e) => {
      resetStallTimer();
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 8); // 0~8% (업로드 단계)
      const uploadedMB = (e.loaded / 1024 / 1024).toFixed(0);
      stepMsgEl.textContent = `서버로 전송 중... ${uploadedMB} / ${fileSizeMB} MB`;
      fillEl.style.width = pct + '%';
    };
    xhr.upload.onload = () => {
      resetStallTimer();
      stepMsgEl.textContent = '전송 완료, 서버 처리 준비 중...';
      fillEl.style.width = '8%';
    };

    // 응답 스트림 처리
    let buf = '';
    xhr.onreadystatechange = () => {
      if (xhr.readyState < 3) return;
      const newText = xhr.responseText.slice(buf.length);
      if (newText) resetStallTimer();
      buf = xhr.responseText;
      const lines = newText.split('\n');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let data;
        try { data = JSON.parse(line.slice(6)); } catch { continue; }
        if (data.step === 'ping') continue;
        if (data.step === 'error') {
          setProgress('error', '오류: ' + data.message);
          fillEl.style.background = '#ef4444';
          analyzeBtn.disabled = false;
          analyzeBtn.textContent = 'AI 피드백 받기';
          done(); return;
        }
        if (data.step === 'done') {
          setProgress('done');
          renderVideoFeedback(data);
          analyzeBtn.disabled = false;
          analyzeBtn.textContent = 'AI 피드백 받기';
          done(); return;
        }
        setProgress(data.step, data.message);
      }
    };

    xhr.onerror = () => {
      setProgress('error', '네트워크 오류가 발생했습니다.');
      fillEl.style.background = '#ef4444';
      analyzeBtn.disabled = false;
      analyzeBtn.textContent = 'AI 피드백 받기';
      done();
    };

    xhr.send(formData);
  });
}

function scoreColor(score) {
  if (score >= 80) return '#10b981';
  if (score >= 60) return '#f59e0b';
  return '#ef4444';
}

function scoreLabel(score) {
  if (score >= 80) return '우수';
  if (score >= 60) return '보통';
  return '개선 필요';
}

function renderScoreBadge(score) {
  const color = scoreColor(score);
  return `<span class="vf-score-badge" style="background:${color}">${score}점 · ${scoreLabel(score)}</span>`;
}

function renderVideoFeedback(data, targetEl) {
  const fb = data.feedback || {};
  const transcript = data.transcript || '';
  const resultEl = targetEl || document.getElementById('vf-result');

  const overallColor = scoreColor(fb.overall_score || 0);

  let html = `
    <div class="report-header no-print" style="display:flex;justify-content:flex-end;margin-bottom:12px;">
      <button class="pdf-btn" onclick="window.print()">📄 PDF 저장</button>
    </div>
    <div class="vf-overall-card" style="border-left:4px solid ${overallColor}">
      <div class="vf-overall-header">
        <span class="vf-overall-label">종합 점수</span>
        <span class="vf-overall-score" style="color:${overallColor}">${fb.overall_score || '-'}점</span>
      </div>
    </div>
  `;

  // 훅 분석
  const hook = fb.hook_analysis || {};
  html += `
    <div class="vf-card">
      <div class="vf-card-header">
        <span>🎣 훅 분석 (초반 30초)</span>
        ${renderScoreBadge(hook.score || 0)}
      </div>
      <div class="vf-card-body">
        <div class="vf-field"><span class="vf-field-label">초반 내용</span><span>${hook.first_30s || '-'}</span></div>
        <div class="vf-field"><span class="vf-field-label">훅 강도</span><span class="vf-tag">${hook.hook_strength || '-'}</span></div>
        <div class="vf-field"><span class="vf-field-label">개선 제안</span><span>${hook.improvement || '-'}</span></div>
      </div>
    </div>
  `;

  // 콘텐츠 흐름
  const flow = fb.content_flow || {};
  html += `
    <div class="vf-card">
      <div class="vf-card-header">
        <span>📊 콘텐츠 흐름</span>
        ${renderScoreBadge(flow.score || 0)}
      </div>
      <div class="vf-card-body">
        <div class="vf-field"><span class="vf-field-label">흐름 요약</span><span>${(flow.summary || '-').replace(/\n/g, '<br>')}</span></div>
        <div class="vf-field"><span class="vf-field-label">핵심 메시지</span><span>${flow.key_message || '-'}</span></div>
        <div class="vf-field"><span class="vf-field-label">템포</span><span class="vf-tag">${flow.pacing || '-'}</span></div>
      </div>
    </div>
  `;

  // CTR 예측
  const ctr = fb.ctr_prediction || {};
  const titles = (ctr.title_suggestion || []).map(t => `<div class="vf-title-item">▶ ${t}</div>`).join('');
  html += `
    <div class="vf-card">
      <div class="vf-card-header">
        <span>🎯 CTR 예측</span>
        ${renderScoreBadge(ctr.score || 0)}
      </div>
      <div class="vf-card-body">
        <div class="vf-field"><span class="vf-field-label">분석</span><span>${ctr.analysis || '-'}</span></div>
        ${titles ? `<div class="vf-field"><span class="vf-field-label">추천 제목</span><div class="vf-titles">${titles}</div></div>` : ''}
      </div>
    </div>
  `;

  // 이탈 위험
  const ret = fb.retention_risk || {};
  const weakPoints = (ret.weak_points || []).map(p => `<li>${p}</li>`).join('');
  html += `
    <div class="vf-card">
      <div class="vf-card-header">
        <span>⚠️ 이탈 위험 구간</span>
        ${renderScoreBadge(ret.score || 0)}
      </div>
      <div class="vf-card-body">
        ${weakPoints ? `<div class="vf-field"><span class="vf-field-label">위험 구간</span><ul class="vf-list">${weakPoints}</ul></div>` : ''}
        <div class="vf-field"><span class="vf-field-label">개선 방안</span><span>${ret.suggestion || '-'}</span></div>
      </div>
    </div>
  `;

  // 잘된 점 / 개선할 점
  const strengths = (fb.strengths || []).map(s => `<li>✅ ${s}</li>`).join('');
  const improvements = (fb.improvements || []).map(i => `<li>💡 ${i}</li>`).join('');
  html += `
    <div class="vf-two-col">
      <div class="vf-card">
        <div class="vf-card-header"><span>👍 잘된 점</span></div>
        <div class="vf-card-body"><ul class="vf-list">${strengths || '<li>-</li>'}</ul></div>
      </div>
      <div class="vf-card">
        <div class="vf-card-header"><span>🔧 개선할 점</span></div>
        <div class="vf-card-body"><ul class="vf-list">${improvements || '<li>-</li>'}</ul></div>
      </div>
    </div>
  `;

  // 자막 원문
  if (transcript) {
    html += `
      <div class="vf-card">
        <div class="vf-card-header"><span>📝 추출된 자막</span></div>
        <div class="vf-card-body"><pre class="vf-transcript">${transcript.replace(/</g, '&lt;')}</pre></div>
      </div>
    `;
  }

  resultEl.innerHTML = html;
  resultEl.classList.remove('hidden');
  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
