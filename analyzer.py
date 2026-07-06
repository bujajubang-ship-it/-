import json
import re
import base64
import httpx
import anthropic
from typing import List, Dict, Optional

# 원고·분석 글쓰기 품질을 위해 Opus 4.8 사용 (가격 $5/$25 per 1M, Sonnet 대비 고급)
WRITER_MODEL = "claude-opus-4-8"


def _safe_json(raw: str, msg=None) -> dict:
    if msg is not None and getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("AI 응답이 너무 길어 잘렸습니다. 입력 내용을 줄이고 다시 시도해주세요.")
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 문자열 내 제어문자 이스케이프
    fixed, in_str, i = [], False, 0
    while i < len(raw):
        c = raw[i]
        if c == '"' and (i == 0 or raw[i-1] != '\\'):
            in_str = not in_str
        if in_str and c == '\n':
            fixed.append('\\n'); i += 1; continue
        if in_str and c == '\r':
            fixed.append('\\r'); i += 1; continue
        if in_str and c == '\t':
            fixed.append('\\t'); i += 1; continue
        fixed.append(c); i += 1
    try:
        return json.loads(''.join(fixed))
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", ''.join(fixed), re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        raise ValueError("AI 응답이 도중에 잘렸습니다. 다시 시도해주세요.")


CHANNEL_GOALS = "이 채널의 정량 목표: 썸네일 CTR 10% 이상, 초반 30초 이탈률 40% 미만. 모든 제목·썸네일·도입부·훅 추천은 이 기준을 달성할 수 있도록 설계하세요."

CONTENT_CREATION_RULES = """
【제목 작성 원칙 — 선택·후회·조건을 강조하라】
클릭률이 높은 제목은 다음 세 가지 심리를 자극한다:
- 선택: "이것만 선택하면 됩니다", "두 가지 중 하나만 고르세요"
- 후회: "이걸 모르면 나중에 후회합니다", "안 샀더니 결국 후회했어요"
- 조건: "이 조건이면 무조건 사세요", "이런 분만 보세요"
→ 제목에 이 세 요소 중 하나 이상을 반드시 포함할 것. 추상적 표현 금지, 시청자 상황을 구체적으로 특정할 것.

【썸네일 문구 원칙 — 결과·감정·공감 중심】
클릭을 유도하는 썸네일 문구는 다음 세 가지 중심이다:
- 결과: "매출 2배", "1년 써보니", "이거 하나로 해결됨" → 구체적 수치·시간·성과
- 감정: 놀람·후회·안도·설렘을 유발하는 문구 → "충격", "반전", "드디어"
- 공감: 시청자가 "맞아, 나도 그래"라고 느끼는 문구 → 흔한 고민·상황을 짧게 압축
→ 경쟁 영상 중 댓글·좋아요 비율이 높은(조회수 대비 반응이 강한) 영상을 벤치마크로 삼아 그 썸네일 패턴을 부자주방 버전으로 변형할 것.

【썸네일 이미지 구성 원칙】
① 줌(zoom)을 당겨서 주제를 화면에 꽉 차게 집중시킬 것 — 배경이 보이지 않을 만큼 클로즈업, 시청자의 시선이 즉시 핵심 피사체로 향하도록
② 시각적 근거를 직관적으로 보여줄 것 — 주제가 제품이면 그 제품 실물을, 결과라면 실제 결과물을 그대로 보여줌. 추상적 이미지 금지, 시청자가 0.5초 안에 무슨 영상인지 알 수 있어야 함
③ 타겟(연령·성별)이 선호하는 이미지 스타일을 사용할 것 — 부자주방 타겟(40~60대 식당 사장님)은 깔끔하고 실용적인 이미지를 선호, 화려한 그래픽보다 실제 주방·제품·사람이 나오는 사진이 효과적
→ visual 필드에 구체적인 촬영 방법(앵글, 거리, 피사체 크기)까지 명시할 것

【도입부 5대 원칙 — 이탈률 40% 미만 달성】
① 썸네일에서 시청자가 기대한 것을 넘어서는 임팩트를 줄 것 (기대 이상의 충격·정보량·공감)
② 썸네일에서 언급한 내용을 도입부 30초 안에 반드시 한 번 더 말할 것 (기대 충족 → 신뢰 형성, 이탈 방지)
③ 시청자가 "맞아, 나도 이 상황이야"라고 강하게 공감하는 장면이나 고민을 먼저 꺼낼 것
④ 재밌거나 임팩트 있는 장면(클라이맥스 B-roll, 놀라운 결과 장면)을 앞으로 당겨서 배치해 이탈률 감소
⑤ 초반 30초 안에 "이 영상을 끝까지 봐야 하는 이유"를 명확하게 전달할 것

【원고(대본) 원칙 — 욕구를 불러일으켜라】
- 물건 소개 영상: 시청자가 그 물건을 당장 사고 싶은 충동이 들도록 구성
  순서: 공감(이 상황 알죠?) → 기존 해결책의 한계(그래서 다들 이렇게 하는데) → 이 제품의 해결 방식(그런데 이게 다르게 해결해요) → 구체적 결과(실제로 ○○ 됩니다) → 상상하게 만들기("사장님 주방에 이게 있으면...")
- 정보 영상: 시청자가 "이 정보를 나만 알고 싶다", "저장해야겠다"는 느낌이 들도록 구성
- 모든 원고는 시청자의 욕구를 자극하는 언어로 작성. 단순 설명이 아닌 감정과 이득을 연결할 것.
"""

CHAT_SYSTEM = """당신은 부자주방 채널 전담 콘텐츠 전략 파트너입니다.

【채널 정보】
채널명: 부자주방
시청자: 외식업 운영자 (식당·분식집·한식당 사장님), 외식업 창업 준비자
제품: 업소용 주방용품 (가스레인지, 업소용 냉장고, 수납용품 등)
서비스: 주방 도면설계, 인테리어 시공
시청자 특성: 가성비·내구성·사용편의에 민감, 실용적이고 구체적인 정보 선호

【채널 정량 목표】
썸네일 CTR: 10% 이상
초반 30초 시청 이탈률: 40% 미만
모든 제목·썸네일·도입부·훅 추천은 이 기준을 달성할 수 있도록 설계

【콘텐츠 전략 원칙】
① 풀링 콘텐츠: 외식업·자영업·창업에 관심 있는 넓은 대중을 끌어당기는 정보성 콘텐츠. 조회수·노출 극대화 목표.
② 키 콘텐츠: 시청자의 실제 문제를 제품(업소용 주방용품)이나 서비스(도면설계·인테리어)로 해결하는 판매 중심 콘텐츠.
③ 발행 비율: 풀링 5 : 키 2
④ 최고의 콘텐츠 = 풀링+키 겸용: 유용한 정보 제공하면서 자연스럽게 제품·서비스 판매까지
⑤ 풀링+키 겸용 주제를 최우선으로 발굴할 것

【제목 원칙 — 선택·후회·조건 강조】
클릭률이 높은 제목은 선택·후회·조건 중 하나 이상을 자극한다.
"이 조건이면 무조건 사세요", "이걸 모르면 후회합니다", "이것만 선택하면 됩니다"

【썸네일 문구 원칙 — 결과·감정·공감 중심】
결과(구체적 수치·성과) / 감정(놀람·후회·안도) / 공감("맞아 나도 그래") 중심으로 작성.
경쟁 영상 중 반응률 높은 영상을 벤치마크해서 부자주방 버전으로 변형 제안.

【도입부 원칙】
① 썸네일 기대를 넘어서는 임팩트 ② 썸네일 언급 내용을 도입부에서 반드시 반복 ③ 강한 공감 장면 먼저
④ 재밌는/임팩트 있는 장면을 앞으로 당겨 이탈률 감소 ⑤ 30초 안에 끝까지 봐야 할 이유 전달
문제제기/공감/손해/이득/사례 중 영상 주제에 맞는 2-3개를 선택해 조합.

【원고 원칙】
시청자의 욕구를 불러일으키는 원고. 물건 소개라면 당장 사고 싶은 충동이 들도록.
공감 → 기존 해결책 한계 → 이 제품의 해결 방식 → 구체적 결과 → 상상하게 만들기 순서로 구성.

【답변 방식】
- 한국어로 답변
- 구체적이고 실행 가능한 조언 (추상적 표현 금지)
- 데이터와 원칙에 근거한 객관적 분석
- 필요하면 제목 샘플, 썸네일 문구, 구성안을 직접 작성해서 제시
- 친근하고 실용적인 톤 유지
- 모르는 것은 모른다고 명확히 말할 것
- 마크다운 굵은 글씨(**텍스트**)와 줄바꿈을 활용해 가독성 있게 작성"""


class Analyzer:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()

    async def _fetch_thumbnail_b64(self, url: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(url)
                return base64.standard_b64encode(r.content).decode()
        except Exception:
            return None

    def _build_videos_text(self, videos: List[Dict], max_videos: int = 10, max_comments: int = 30) -> str:
        parts = []
        for i, v in enumerate(videos[:max_videos], 1):
            comments_block = "\n".join(
                f"  [{c['like_count']}좋아요] {c['text']}"
                for c in v.get("comments", [])[:max_comments]
            )
            heatmap_block = v.get("heatmap_summary", "")
            parts.append(
                f"[영상{i}] {v['title']}\n"
                f"조회수:{v['view_count']:,} / 좋아요:{v['like_count']:,} / 댓글:{v['comment_count']:,}\n"
                f"채널:{v['channel']} / 업로드:{v['published_at']}\n"
                f"URL:{v['url']}\n"
                f"설명:{v['description'][:300]}\n"
                + (f"{heatmap_block}\n" if heatmap_block else "")
                + f"인기댓글(좋아요순):\n{comments_block or '  (댓글 없음)'}\n"
            )
        return "\n---\n".join(parts)

    def _build_videos_simple(self, videos: List[Dict]) -> str:
        parts = []
        for i, v in enumerate(videos[:20], 1):
            parts.append(
                f"[{i}] {v['title']}\n"
                f"조회수:{v['view_count']:,} / 채널:{v['channel']} / 업로드:{v['published_at']}\n"
                f"설명:{v['description'][:250]}\n"
            )
        return "\n---\n".join(parts)

    def _build_naver_text(self, naver: List[Dict]) -> str:
        if not naver:
            return "데이터 없음"
        lines = [f"[{r['cafe_name']}] {r['title']} — {r['description']}" for r in naver[:40]]
        return "\n".join(lines)

    async def analyze(self, keyword: str, videos: List[Dict], naver: List[Dict]) -> Dict:
        videos_text = self._build_videos_text(videos)
        naver_text = self._build_naver_text(naver)

        # Fetch top-5 thumbnails for visual analysis
        thumb_blocks = []
        for v in videos[:5]:
            url = v.get("thumbnail_url", "")
            if url:
                b64 = await self._fetch_thumbnail_b64(url)
                if b64:
                    thumb_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                    })

        system_prompt = (
            "당신은 유튜브 콘텐츠 전략 전문가입니다. "
            "주어진 데이터를 깊이 분석하여 크리에이터가 바로 활용할 수 있는 실질적인 인사이트를 제공합니다. "
            "이 채널의 정량 목표: 썸네일 CTR 10% 이상, 초반 30초 이탈률 40% 미만. "
            "모든 제목·썸네일·도입부 추천은 이 기준을 달성할 수 있도록 설계하세요. "
            "영상 데이터에 'Most Replayed 핫스팟'이 포함된 경우, 시청자가 어느 구간에서 집중했는지 분석하여 "
            "편집 전략과 콘텐츠 구성에 반영하세요. 핫스팟 타임스탬프와 영상 제목/설명을 교차 분석해 "
            "어떤 내용이 시청자를 사로잡았는지 추론하세요. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""키워드: "{keyword}"

== 유튜브 상위 영상 ==
{videos_text}

== 네이버 카페 반응 ==
{naver_text}

위 데이터를 분석하여 아래 JSON 형식으로 한국어 리포트를 작성하세요.
배열의 각 항목은 구체적이고 실용적으로 작성하세요 (추상적 표현 금지).

{{
  "summary": "3-4문장으로 시장 상황과 핵심 인사이트 요약",
  "one_line_concept": "이 영상의 핵심 컨셉 한 줄 (크리에이터가 영상 방향 잡는 데 쓸 문장)",
  "desire_analysis": {{
    "curiosity": ["구체적 궁금증 6-8개 (예: 가스레인지 1구vs2구 어느게 더 효율적인지)"],
    "complaints": ["구체적 불만/페인포인트 6-8개 (예: 청소할 때 버너 사이 기름때 빠지는 법 없음)"],
    "wants": ["시청자가 원하는 것 6-8개 (예: 초보도 따라할 수 있는 단계별 설명)"]
  }},
  "top_questions": [
    "실제 댓글/카페 데이터 기반 TOP10 질문 (물음표로 끝낼 것)"
  ],
  "competitor_analysis": {{
    "title_patterns": ["제목 패턴 4-5개 (패턴명+예시+왜 효과적인지)"],
    "thumbnail_styles": ["썸네일 공통 요소 3-5개 (색상/텍스트/구도/표정 등 구체적으로)"],
    "popular_keywords": ["자주 등장하는 핵심 키워드 12-15개"],
    "content_gaps": ["경쟁 영상이 다루지 않은 빈틈 5-7개 (구체적 주제로)"]
  }},
  "top_videos": [
    {{
      "title": "영상 제목",
      "views": 조회수숫자,
      "url": "https://...",
      "thumbnail": "썸네일URL",
      "success_reason": "이 영상이 잘 된 핵심 이유 1-2문장"
    }}
  ],
  "recommended_titles": [
    {{
      "title": "클릭률 최적화 제목",
      "hook_reason": "왜 클릭하고 싶어지는지 이유",
      "target_emotion": "유발하는 감정 (호기심/불안해소/욕망 등)"
    }}
  ],
  "must_include_content": ["반드시 넣어야 할 내용 7-10개 (추상적 말고 실제 섹션/장면 단위로)"],
  "differentiation_points": ["차별화 포인트 5-7개 (왜 이게 차별화인지 근거 포함)"],
  "recommended_structure": [
    "영상 구성 단계별 (예: [0:00] 후킹 - 시청자가 가장 궁금해하는 문제 제시)"
  ],
  "heatmap_insights": {{
    "available": true,
    "pattern_summary": "경쟁 영상들의 Most Replayed 패턴 종합 (예: 상위 영상 3개 모두 3~5분 구간에서 최고 강도 → 핵심 정보를 3분 안에 배치 권장)",
    "hot_moments": [
      "영상별 핫스팟 해석 (예: 영상1 2:30~3:45 강도91% → 제목에서 언급한 비교 결과 공개 시점으로 추정)"
    ],
    "editor_tips": [
      "히트맵 패턴에서 도출한 편집 전략 3-5개 (예: 초반 30초 안에 핵심 결론 예고 필수, 가격 공개는 영상 전반부 배치 등)"
    ]
  }}
}}"""

        content: list = []
        if thumb_blocks:
            content.append({"type": "text", "text": "상위 영상 썸네일 (썸네일 스타일 분석에 반영):\n"})
            content.extend(thumb_blocks)
        content.append({"type": "text", "text": user_text})

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        raw = msg.content[0].text.strip()
        return _safe_json(raw)

    async def analyze_planning(self, keyword: str, product_desc: str, market_insights: str) -> Dict:
        system_prompt = (
            "당신은 유튜브 영상 기획 전문가입니다. "
            "시장 데이터를 바탕으로 문제를 정의하고, CTR 10% 이상을 목표로 제목과 썸네일을 기획합니다. "
            "크리에이터가 촬영 전에 확정할 수 있도록 구체적이고 바로 쓸 수 있는 결과물을 만드세요. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""영상 키워드: "{keyword}"
내 제품/서비스/채널: {product_desc}

== 시장조사에서 파악한 시청자 욕구/불만/궁금증 ==
{market_insights or "별도 시장조사 데이터 없음 — 키워드 기반으로 분석"}

위 정보를 바탕으로 아래 JSON 형식으로 영상 기획안을 작성하세요.
추상적 표현 금지. 실제로 촬영 전에 확정할 수 있는 수준으로 구체적으로 작성하세요.

{{
  "problem_definition": {{
    "current_situation": "시청자가 지금 처한 현실/상황 (현상) — 구체적 묘사",
    "desired_outcome": "시청자가 진짜 원하는 결과 (욕구) — 구체적 묘사",
    "core_problem": "현상과 욕구 사이에 가로막힌 핵심 문제 — 한 문장",
    "solution_angle": "이 제품/채널로 그 문제를 해결하는 영상 각도 — 구체적으로"
  }},
  "recommended_titles": [
    {{
      "title": "제목 (30자 내외, 한국어)",
      "ctr_strategy": "이 제목이 클릭을 유도하는 심리 전략 (공포/호기심/이득/긴급성/비교 등)",
      "hook_reason": "시청자가 클릭하고 싶어지는 이유 한 줄",
      "strength": "이 제목의 강점"
    }}
  ],
  "thumbnail_concepts": [
    {{
      "concept_name": "썸네일 컨셉 이름",
      "main_text": "썸네일 메인 문구 (크고 강렬하게, 5-10자)",
      "sub_text": "서브 문구 (있다면, 없으면 빈 문자열)",
      "visual": "이미지/배경/색상/구도 설명 (촬영자가 바로 재현할 수 있도록 구체적으로)",
      "expression": "표정/제스처/포즈 (사람이 나오는 경우)",
      "color_mood": "주요 색상과 분위기 (예: 빨간 배경 + 흰 텍스트 + 충격 표정)",
      "why_clicks": "이 썸네일이 클릭을 유도하는 심리적 이유"
    }}
  ]
}}

recommended_titles는 5개, thumbnail_concepts는 3개 작성하세요."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    async def write_intro(self, keyword: str, product_desc: str, problem_definition: str, viewer_desire: str) -> Dict:
        system_prompt = (
            "당신은 유튜브 영상 도입부 전문 작가입니다. "
            "문제제기 → 공감 → 손해 → 이득 → 사례 공식으로 시청자를 30초 안에 사로잡는 도입부를 작성합니다. "
            "실제로 카메라 앞에서 말할 수 있는 자연스러운 구어체 한국어로 작성하세요. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""영상 키워드: "{keyword}"
내 제품/서비스/채널: {product_desc}
문제 정의: {problem_definition}
시청자가 원하는 것: {viewer_desire}

아래 5단계 공식으로 도입부를 작성하세요.
각 단계가 자연스럽게 이어지도록 구어체로 작성하고, 전체 30-50초 분량으로 만드세요.

{{
  "full_intro": "전체 도입부 대본 (각 단계가 자연스럽게 이어지는 완성본, 구어체)",
  "breakdown": [
    {{
      "stage": "문제제기",
      "text": "이 파트 대본",
      "purpose": "시청자가 느끼는 감정/반응",
      "duration_sec": 예상초수(숫자)
    }},
    {{
      "stage": "공감",
      "text": "이 파트 대본",
      "purpose": "시청자가 느끼는 감정/반응",
      "duration_sec": 예상초수(숫자)
    }},
    {{
      "stage": "손해",
      "text": "이 파트 대본 (이걸 모르면/안 하면 어떤 손해가 생기는지)",
      "purpose": "시청자가 느끼는 감정/반응",
      "duration_sec": 예상초수(숫자)
    }},
    {{
      "stage": "이득",
      "text": "이 파트 대본 (이 영상을 끝까지 보면 얻는 것)",
      "purpose": "시청자가 느끼는 감정/반응",
      "duration_sec": 예상초수(숫자)
    }},
    {{
      "stage": "사례",
      "text": "이 파트 대본 (실제 사례/증거/경험)",
      "purpose": "시청자가 느끼는 감정/반응",
      "duration_sec": 예상초수(숫자)
    }}
  ],
  "hook_variations": [
    "첫 문장만 다르게 쓴 대안 훅 3개 (A/B 테스트용)"
  ],
  "filming_tips": [
    "이 도입부를 촬영할 때 주의할 점 3개 (표정/속도/배경 등 실용적 팁)"
  ]
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    async def write_script(self, keyword: str, product_desc: str, reference_script: str, context: str) -> Dict:
        system_prompt = (
            "당신은 유튜브 영상 대본 작가입니다. "
            "잘 된 영상 대본의 구조와 흐름을 분석하고, 내 제품/주제에 맞게 변형하여 "
            "시청자가 더 좋아할 수 있도록 책/전문가/시연 요소를 추가하고 댓글 유도로 마무리합니다. "
            "실제로 촬영할 수 있는 구어체 한국어로 작성하세요. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""영상 키워드: "{keyword}"
내 제품/서비스/채널: {product_desc}
추가 컨텍스트 (문제 정의/시청자 욕구 등): {context or "없음"}

== 레퍼런스 대본 (잘 된 영상) ==
{reference_script}

레퍼런스 대본의 구조를 분석하고, 내 주제에 맞게 변형한 대본을 작성하세요.
시청자가 더 좋아할 수 있도록 책/전문가 의견/시연 장면 등을 추가 제안하고,
마지막에 댓글을 유도하는 강력한 마무리를 포함하세요.

{{
  "reference_structure_analysis": [
    "레퍼런스 대본의 구조 분석 (섹션별로 어떤 역할을 하는지)"
  ],
  "adapted_script": "변형된 전체 대본 (구어체, 내 주제에 맞게 변형, 섹션 구분 명시)",
  "sections": [
    {{
      "section_name": "섹션 이름",
      "original_approach": "레퍼런스에서 가져온 구조/방식",
      "my_version": "내 주제에 맞게 변형한 방향",
      "script": "이 섹션의 대본"
    }}
  ],
  "enhancement_suggestions": [
    {{
      "type": "책/전문가/시연/통계/사례 중 하나",
      "suggestion": "구체적으로 어떤 내용을 어느 부분에 추가하면 좋은지",
      "why": "이걸 추가하면 시청자에게 어떤 효과가 있는지"
    }}
  ],
  "comment_inducing": {{
    "strategy": "이 영상에서 댓글을 유도하는 핵심 전략",
    "ending_script": "영상 마지막 댓글 유도 대본 (구어체, 시청자가 대답하고 싶어지는 질문 포함)",
    "question_variations": [
      "댓글 유도 질문 3개 (각각 다른 방식 — 경험 공유/의견/투표 등)"
    ]
  }}
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    def _kb_text(self, knowledge: Optional[List[Dict]], per: int = 500, budget: int = 4500) -> str:
        """활성 지식을 프롬프트에 넣을 텍스트로 — 프롬프트 비대/속도저하 방지 위해 항목당·전체 길이 캡."""
        if not knowledge:
            return ""
        out, used = [], 0
        for k in knowledge:
            s = (k.get("summary") or (k.get("content") or "")).strip()[:per]
            if not s:
                continue
            block = f"[{k.get('title','강의')}]\n{s}"
            if used + len(block) > budget:
                break
            out.append(block)
            used += len(block)
        return "\n".join(out)

    def _build_viewtrap_text(self, refs: Optional[Dict]) -> str:
        if not refs:
            return ""
        top = refs.get("top_videos") or []
        hot = refs.get("hot_videos") or []
        lines = []
        if top:
            lines.append("【ViewTrap 성과 영상 — 이미 검증된 고성과 패턴】")
            for i, v in enumerate(top[:10], 1):
                lines.append(
                    f"[성과{i}] {v.get('title','')}\n"
                    f"  채널:{v.get('channel','')} / 조회수:{v.get('views',0):,} / 성과:{v.get('performance_rate_str','')}\n"
                    f"  URL:{v.get('url','')}"
                )
        if hot:
            lines.append("\n【ViewTrap 핫비디오 — 최근 신규 고성과 영상】")
            for i, v in enumerate(hot[:10], 1):
                lines.append(
                    f"[핫{i}] {v.get('title','')}\n"
                    f"  채널:{v.get('channel','')} / 조회수:{v.get('views',0):,} / 성과:{v.get('performance_rate_str','')}\n"
                    f"  URL:{v.get('url','')}"
                )
        return "\n".join(lines)

    async def analyze_midform(self, keyword: str, product_desc: str, videos: List[Dict], naver: List[Dict], viewtrap_refs: Optional[Dict] = None, knowledge: List[Dict] = None) -> Dict:
        videos_text = self._build_videos_text(videos)
        naver_text = self._build_naver_text(naver)
        kt = ""
        kb_txt = self._kb_text(knowledge)
        if kb_txt:
            kt = ("\n== ★ 사장님 영상 제작·키 컨텐츠 강의 (제목·도입부·원고 작성 시 이 작성법을 적용) ==\n"
                  + kb_txt
                  + "\n[적용] 위 강의의 작성 원리를 제목·썸네일 문구·도입부·원고에 실제로 녹이되, 억지로 끼워넣지는 마세요.\n")

        thumb_blocks = []
        for v in videos[:3]:
            url = v.get("thumbnail_url", "")
            if url:
                b64 = await self._fetch_thumbnail_b64(url)
                if b64:
                    thumb_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})

        system_prompt = (
            "당신은 유튜브 영상 기획 전문가입니다. "
            "채널: 부자주방 — 외식업 운영자(식당·분식집·한식당 사장님) 대상 업소용 주방용품 전문 채널. "
            "제품: 업소용 주방용품. 서비스: 주방 도면설계, 인테리어 시공. "
            "시청자: 가성비·내구성·사용편의에 민감한 식당 사장님들. "
            "\n\n"
            "【콘텐츠 유형 이해】\n"
            "- 풀링 컨텐츠: 외식업·자영업·창업 대중을 끌어들이는 정보성 콘텐츠 (조회수 목표)\n"
            "- 키 컨텐츠: 시청자 문제를 제품·서비스로 해결하는 판매 목적 콘텐츠\n"
            "- 최고 = 풀링+키 겸용: 정보 제공하면서 자연스럽게 제품·서비스로 연결\n\n"
            "시장 데이터를 분석하여 제목부터 전체 원고까지 영상 제작의 모든 단계를 한 번에 완성합니다. "
            "도입부는 문제제기/공감/손해/이득/사례 중 이 영상 주제에 가장 잘 맞는 요소 2-3개를 선택해 자연스럽게 조합하세요. "
            "5단계를 모두 순서대로 쓰는 것이 아니라, 상황에 맞는 요소를 골라 결합하는 것이 핵심입니다. "
            "전체 원고는 실제 카메라 앞에서 말할 수 있는 구어체로 작성하세요. "
            f"{CHANNEL_GOALS}"
            f"{CONTENT_CREATION_RULES}"
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        viewtrap_text = self._build_viewtrap_text(viewtrap_refs)
        product_info = f"\n이번 영상 제품 상세 정보:\n{product_desc}" if product_desc.strip() else ""
        user_text = f"""영상 키워드: "{keyword}"{product_info}

== 유튜브 상위 영상 데이터 ==
{videos_text}

== 네이버 카페 반응 ==
{naver_text}
{f'''
== ViewTrap 성과/핫비디오 레퍼런스 ==
{viewtrap_text}

[ViewTrap 활용 지침]
- 성과 영상과 핫비디오의 제목 패턴·썸네일 구성·영상 구조를 분석하세요
- 이미 검증된 성공 패턴을 이 영상의 주제({keyword})에 맞게 카피·변형하여 적용하세요
- 제목에서: 성과 영상 제목의 언어 패턴(단어 선택, 문장 구조)을 부자주방 주제로 변환
- 썸네일에서: 핫비디오 썸네일 패턴을 분석해 동일한 심리 자극 방식 적용
- viewtrap_insights 필드에 어떤 영상을 참고했고 어떻게 변형 적용했는지 명시하세요
''' if viewtrap_text else ''}
{kt}
위 시장 데이터를 바탕으로 영상 제작의 모든 단계를 포함한 완성된 기획안을 작성하세요.

[벤치마크 분석 — 먼저 할 것]
유튜브 상위 영상 데이터에서 댓글수/좋아요수 비율이 조회수 대비 높은 영상(반응률이 좋은 영상)을 1개 찾아 benchmark_video 필드에 기록하세요.
그 영상의 썸네일 패턴(결과/감정/공감 중 무엇을 사용했는지)을 분석해 우리 채널 버전으로 변형하여 thumbnails에 반영하세요.

[제목 작성 규칙]
- 선택·후회·조건 중 하나 이상을 반드시 포함할 것
- 예: "이 조건이면 무조건 사세요", "안 샀더니 결국 후회", "이것만 선택하면 됩니다"
- title_keyword_type 필드에 선택/후회/조건 중 어떤 것을 사용했는지 명시

[썸네일 문구 규칙]
- 결과(구체적 수치·성과)·감정(놀람·후회·안도)·공감("맞아 나도 그래") 중심
- emotion_type 필드에 결과/감정/공감 중 어떤 것을 사용했는지 명시

[도입부 규칙 — 반드시 지킬 것]
① 썸네일에서 시청자가 기대한 것을 넘어서는 임팩트를 줄 것
② 썸네일 메인 문구에서 언급한 내용을 도입부 30초 안에 반드시 한 번 더 말할 것 → thumbnail_callback 필드에 명시
③ 시청자 공감 장면을 먼저 배치할 것 — 이 공감은 반드시 empathy_points(실제 댓글/카페 인용)에 기반할 것. 지어낸 공감 금지.
④ 재밌거나 임팩트 있는 장면(클라이맥스 결과 장면)을 앞으로 당겨서 이탈률 감소 → impact_scene_first 필드에 어떤 장면을 앞으로 당길지 명시
⑤ 30초 안에 "끝까지 봐야 하는 이유"를 전달할 것

[원고 규칙]
- 물건 소개: 공감 → 기존 해결책 한계 → 이 제품의 해결 방식 → 구체적 결과 → 상상하게 만들기 순서로 욕구 자극
- 정보 영상: "이 정보를 나만 알고 싶다"는 느낌이 들도록 구성
- 모든 대본은 시청자의 욕구를 불러일으키는 언어 사용

[도입부 공식 선택 원칙]
- 정보/꿀팁 콘텐츠 → 공감 + 이득 조합 추천
- 신제품 소개 → 문제제기 + 이득 + 사례 조합 추천
- 비교/검증 → 공감 + 손해 + 이득 조합 추천
- 어떤 요소를 선택했는지 formula 필드에 명시하고, 이유도 설명할 것

[공감 포인트 — 반드시 실제 데이터에서 찾을 것 (가장 중요)]
공감은 지어내지 말고, 위 유튜브 인기댓글(좋아요순)·네이버 카페 글에서 실제로 사람들이 토로한 고민·불만·욕구를 근거로 잡으세요.
empathy_points에 실제 문장을 인용하고, 도입부(intro)와 원고(script)의 공감 장면을 이 실제 데이터에 기반해 작성하세요. 데이터가 없으면 "추정"이라 표시하세요.

{{
  "concept": "이 영상의 핵심 컨셉 한 줄 (제작 방향 잡는 문장)",
  "empathy_points": [
    {{
      "quote": "실제 유튜브 인기댓글 또는 네이버 카페에서 가져온 원문 (좋아요·공감 많이 받은 고민/불만/욕구)",
      "source": "출처 (유튜브 인기댓글 / 네이버 카페 등)",
      "insight": "이 글에서 읽어낸 시청자의 진짜 속마음",
      "use_in_intro": "이 공감 포인트를 도입부에서 어떻게 쓸지 (실제 멘트로)"
    }}
  ],
  "content_type": "풀링+키 또는 풀링 또는 키",
  "content_type_reason": "왜 이 유형인지, 풀링+키라면 어떻게 자연스럽게 판매로 연결되는지",
  "sell_angle": "이 영상으로 자연스럽게 노출할 제품·서비스 (풀링 단독이면 빈 문자열)",
  "benchmark_video": {{
    "title": "벤치마크로 선택한 경쟁 영상 제목",
    "reason": "왜 이 영상을 벤치마크로 선택했는지 (반응률이 높은 이유)",
    "thumbnail_pattern": "이 영상 썸네일의 패턴 분석 (결과/감정/공감 중 무엇, 어떤 문구 사용)",
    "our_version": "이 패턴을 부자주방 버전으로 변형하면?"
  }},
  "market_summary": "시장 상황과 시청자 핵심 욕구 2-3문장",
  "viewer_desires": {{
    "curiosity": ["구체적 궁금증 5-6개"],
    "complaints": ["구체적 불만/페인포인트 5-6개"],
    "wants": ["시청자가 원하는 것 5-6개"]
  }},
  "titles": [
    {{
      "title": "제목 (30자 내외, 선택·후회·조건 중 하나 포함)",
      "title_keyword_type": "선택 또는 후회 또는 조건",
      "strategy": "클릭 심리 전략",
      "hook_reason": "클릭하고 싶어지는 이유"
    }}
  ],
  "thumbnails": [
    {{
      "main_text": "썸네일 메인 문구 (5-10자, 결과·감정·공감 중심)",
      "emotion_type": "결과 또는 감정 또는 공감",
      "sub_text": "서브 문구 (없으면 빈 문자열)",
      "zoom_subject": "줌으로 당겨서 꽉 채울 피사체 (예: 가스레인지 버너 클로즈업, 청소 전후 비교면 클로즈업)",
      "visual_evidence": "시각적 근거 — 주제를 직관적으로 증명하는 실물 이미지 설명 (추상적 표현 금지, 0.5초 안에 무슨 영상인지 알 수 있게)",
      "target_image_style": "40-60대 식당 사장님 타겟에 맞는 이미지 스타일 (실제 주방/제품/사람 사진 기반으로 구체적으로)",
      "visual": "최종 촬영 방법 (앵글·거리·피사체 크기·구도를 구체적으로, 촬영자가 바로 재현 가능하게)",
      "color_mood": "주요 색상과 분위기",
      "expression": "표정/포즈 (사람이 나오는 경우)",
      "why_clicks": "클릭 유도 이유"
    }}
  ],
  "problem_definition": {{
    "viewer_situation": "시청자가 지금 처한 상황",
    "core_desire": "시청자가 진짜 원하는 결과",
    "video_angle": "이 영상이 문제를 해결하는 각도"
  }},
  "intro": {{
    "formula": "선택한 공식 요소들 (예: 공감 + 손해 + 이득)",
    "reason": "이 공식을 선택한 이유",
    "thumbnail_callback": "도입부에서 썸네일 문구를 다시 언급하는 방식 (예: '제가 썸네일에서 말한 ○○, 사실 이게 핵심이에요')",
    "impact_scene_first": "이탈률 낮추기 위해 앞으로 당길 임팩트 장면 (예: 완성된 결과물 장면, 가장 놀라운 비교 장면)",
    "script": "완성된 도입부 대본 (30-50초 분량, 구어체, 임팩트 장면 + 썸네일 콜백 + 공감 포함)",
    "hook_line": "첫 문장 (스크롤을 멈추게 하는)"
  }},
  "script_sections": [
    {{
      "name": "섹션명",
      "timestamp": "00:00-01:30",
      "content": "이 섹션에서 다룰 핵심 내용",
      "script": "실제 대본 (구어체, 욕구 자극 언어 사용, 100-200자)",
      "filming_tip": "이 장면 촬영 팁"
    }}
  ],
  "cta": "영상 마지막 댓글/구독 유도 대본 (구어체)",
  "estimated_duration": "예상 영상 길이 (예: 5-7분)",
  "must_include": ["반드시 넣어야 할 내용 6-8개"],
  "differentiation": ["차별화 포인트 4-5개 (근거 포함)"],
  "viewtrap_insights": {{
    "referenced_videos": ["참고한 ViewTrap 영상 제목 (없으면 빈 배열)"],
    "applied_patterns": "어떤 패턴을 어떻게 이 영상에 적용했는지 설명 (ViewTrap 데이터가 없으면 빈 문자열)"
  }},
  "youtube_description": "유튜브 영상 설명글. 반드시 첫 줄을 #키워드 (핵심 명사만, 예: #자동숯불구이기) 로 시작. 그 다음 줄부터 키워드를 8회 이상 자연스럽게 반복한 본문 5-7문장. 이후 고정 링크 블록을 그대로 포함:\\n\\n🔗 {keyword} 자세히 보기\\n👉 부자주방 자사몰 : https://bujaikm.com/\\n\\n🛒 네이버에서 바로 보기\\n👉 부자주방 스마트스토어 : https://smartstore.naver.com/bujakitchen\\n\\n📞 문의가 필요하시면\\n부자주방 1600-6787 으로 편하게 연락 주세요.",
  "instagram_caption": "인스타그램 캡션. 반드시 첫 줄을 #키워드 (핵심 명사만) 로 시작. 그 다음 줄부터 키워드를 8회 이상 자연스럽게 반복한 본문 5-7문장 (유튜브 설명글과 다른 문장으로). 이후 고정 링크 블록:\\n\\n🔗 {keyword} 자세히 보기\\n👉 부자주방 자사몰 : https://bujaikm.com/\\n\\n🛒 네이버에서 바로 보기\\n👉 부자주방 스마트스토어 : https://smartstore.naver.com/bujakitchen\\n\\n📞 문의가 필요하시면\\n부자주방 1600-6787 으로 편하게 연락 주세요."
}}

titles는 5개, thumbnails는 3개, script_sections는 영상 흐름에 맞게 4-7개 작성하세요.
youtube_description과 instagram_caption은 키워드({keyword})를 본문에 최소 8회 이상 자연스럽게 포함하고 첫 줄은 반드시 #키워드 형태로 시작하세요."""

        content: list = []
        if thumb_blocks:
            content.append({"type": "text", "text": "상위 영상 썸네일 참고:\n"})
            content.extend(thumb_blocks)
        content.append({"type": "text", "text": user_text})

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_topic_trends(self, youtube_data: List[Dict], naver_data: List[Dict]) -> Dict:
        videos_text = self._build_videos_simple(youtube_data)
        naver_text = self._build_naver_text(naver_data)

        system_prompt = (
            "당신은 외식업 주방용품 유튜브 채널 전략 컨설턴트입니다. "
            "채널: 부자주방 — 외식업 운영자(식당·분식집·한식당 사장님) 대상 업소용 주방용품 전문 채널. "
            "제품: 업소용 주방용품. 서비스: 주방 도면설계, 인테리어 시공. "
            "\n\n"
            "【부자주방 콘텐츠 전략】\n"
            "① 풀링 컨텐츠: 외식업·자영업·창업에 관심 있는 넓은 대중을 끌어당기는 정보성 콘텐츠. "
            "조회수·노출 극대화 목표. 주제를 넓게 잡아 더 많은 사람이 채널로 유입되게 함.\n"
            "② 키 컨텐츠: 시청자의 실제 문제·불만을 제품(업소용 주방용품)이나 서비스(도면설계·인테리어)로 "
            "해결하는 판매 중심 콘텐츠.\n"
            "③ 발행 비율 목표: 풀링 5 : 키 2\n"
            "④ 최고의 콘텐츠 = 풀링+키 겸용: 유용한 정보 제공(풀링 효과)하면서 "
            "자연스럽게 제품·서비스 판매까지(키 효과). "
            "예: 주방동선 설계 꿀팁 = 정보성(풀링) + 도면설계 서비스·주방용품 자연 판매(키). "
            "이런 겸용 주제를 최우선으로 발굴하세요.\n\n"
            "추천 근거는 반드시 데이터에서 발견한 실제 증거를 포함해야 합니다. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""== 유튜브 트렌드 데이터 (경쟁 채널 + 인기 영상) ==
{videos_text}

== 네이버 카페 데이터 (식당 사장님 커뮤니티 트렌드) ==
{naver_text}

위 데이터를 분석하여 부자주방 채널에서 지금 바로 촬영해야 할 주제를 추천하세요.

[추천 기준]
- 콘텐츠 유형 분류 필수: 각 주제가 풀링/키/풀링+키 중 어디에 해당하는지 근거와 함께 명시
- 풀링+키 겸용 주제를 최우선으로 찾아낼 것 (가장 가치 높음)
- 데이터에서 실제로 발견한 트렌드 근거를 포함할 것
- 경쟁 영상이 아직 못 다룬 빈틈을 찾을 것

{{
  "trend_summary": "현재 외식업 주방용품 시장 핵심 트렌드 요약 2-3문장 (데이터 기반)",
  "content_mix_note": "추천 주제들의 풀링/키/겸용 구성 및 발행 순서 제안 (풀링5:키2 비율 고려)",
  "hot_topics": [
    {{
      "title": "바로 쓸 수 있는 영상 제목 (구체적, 30자 내외)",
      "keyword": "미드폼 탭에 입력할 검색 키워드 (짧게)",
      "content_type": "풀링+키 또는 풀링 또는 키",
      "content_type_reason": "왜 이 유형인지 한 문장 (어떻게 풀링이 되는지, 어떻게 판매로 연결되는지)",
      "sell_angle": "이 영상으로 자연스럽게 판매할 제품·서비스 (풀링 단독이면 빈 문자열)",
      "why_now": "왜 지금 이 주제가 뜨는지 구체적 근거 (어느 데이터에서 발견했는지 포함)",
      "viewer_pain": "이 주제가 해결하는 시청자의 실제 고민",
      "content_gap": "경쟁 채널이 아직 못 다룬 빈틈",
      "urgency": "high 또는 medium 또는 low",
      "urgency_reason": "긴급도 이유"
    }}
  ],
  "cafe_insights": "네이버 카페에서 발견한 식당 사장님들의 핵심 고민/관심사 3-4가지",
  "competitor_insights": "경쟁 채널 최근 동향 및 부자주방이 치고 들어갈 틈",
  "avoid_topics": ["이미 포화되어 새로 만들어도 묻힐 주제 3-4개 (구체적으로)"]
}}

hot_topics는 5개 작성하세요: 풀링+키 겸용 2개 이상, 풀링 2개, 키 1개. urgency high를 앞에 배치. 각 필드는 간결하게 1-2문장 이내로."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_shortform(self, keyword: str, product_desc: str, duration: str, videos: List[Dict] = None, naver: List[Dict] = None, knowledge: List[Dict] = None) -> Dict:
        market_section = ""
        if videos:
            market_section += f"\n== 유튜브 시장 데이터 (시청자 욕구·관심사 분석용) ==\n{self._build_videos_text(videos)}\n"
        if naver:
            market_section += f"\n== 네이버 카페 반응 ==\n{self._build_naver_text(naver)}\n"
        kb_txt = self._kb_text(knowledge)
        if kb_txt:
            market_section += ("\n== ★ 사장님 원고·바이럴 강의 (훅·나레이션·자막 작성 시 이 원리 적용) ==\n"
                               + kb_txt
                               + "\n[적용] 위 강의 원리(고객언어·짜치는=날것·전염·가십·못난이 정서 등) 중 이 숏폼에 가장 강력한 2~3개를 골라 훅·나레이션·자막에 진하게 녹이세요.\n")

        system_prompt = (
            "당신은 인스타그램 릴스 전문 콘텐츠 전략가입니다. "
            "채널: 부자주방 — 외식업 운영자(식당·분식집·한식당 사장님) 대상 업소용 주방용품 전문 채널. "
            "시장 데이터(유튜브 댓글, 네이버 카페)를 분석해 시청자가 진짜 원하는 것을 파악하고, "
            "인스타그램 알고리즘에서 저장·공유·댓글이 노출을 결정한다는 것을 알고, "
            "이 세 가지 지표를 극대화하는 숏폼 콘텐츠를 기획합니다. "
            "첫 1-3초 훅이 스크롤을 멈추게 해야 하며, 자막/텍스트 오버레이로 음소거 시청도 소화 가능해야 합니다. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        product_info = f"\n이번 릴스 제품 상세: {product_desc}" if product_desc.strip() else ""
        user_text = f"""주제/키워드: "{keyword}"{product_info}
영상 길이: {duration}초
{market_section or "시장 데이터 없음 — 키워드 기반으로 분석"}

인스타그램 릴스 알고리즘 핵심: 저장 > 공유 > 댓글 > 좋아요 순으로 노출에 영향.
위 시장 데이터와 정보를 바탕으로 아래 JSON 형식으로 숏폼 기획안을 작성하세요.

[공감 포인트 — 반드시 실제 데이터에서 찾을 것 (가장 중요)]
공감은 지어내지 말고, 위 유튜브 인기댓글(좋아요순)·네이버 카페 글에서 실제로 사람들이 토로한 고민·불만·욕구를 근거로 잡으세요.
empathy_points에 실제 댓글/카페 문장을 인용하고, 그것을 훅·나레이션·자막에 녹이세요. 데이터가 없으면 솔직히 "추정"이라고 표시하세요.

{{
  "empathy_points": [
    {{
      "quote": "실제 유튜브 댓글 또는 네이버 카페에서 가져온 원문 (사람들이 공감·좋아요 많이 한 고민/불만/욕구)",
      "source": "출처 (예: 유튜브 인기댓글 / 네이버 카페)",
      "insight": "이 글에서 읽어낸 시청자의 진짜 속마음",
      "use_in": "이 공감 포인트를 영상 어디에 어떻게 쓸지 (훅/도입/자막 등)"
    }}
  ],
  "core_message": "이 릴스 하나로 전달할 핵심 메시지 한 문장 (시청자가 저장하고 싶어지는 유용한 내용)",
  "hooks": [
    {{
      "text": "첫 1-3초 훅 문장 (스크롤을 멈추게 하는 강렬한 한 줄)",
      "type": "훅 유형 (충격/공감/질문/비밀공개/반전 중 하나)",
      "why": "왜 스크롤을 멈추게 하는지 이유"
    }}
  ],
  "script": [
    {{
      "time": "00:00-00:03",
      "scene": "장면 설명 (무엇을 찍는지)",
      "narration": "나레이션/말 (없으면 빈 문자열)",
      "text_overlay": "화면에 올릴 텍스트 자막 (굵고 짧게, 음소거 시청자용)",
      "action": "행동/연출 팁"
    }}
  ],
  "save_triggers": [
    "저장을 유도하는 요소 3-4개 (예: 나중에 써먹을 수 있는 팁 형식으로 구성)"
  ],
  "share_triggers": [
    "공유를 유도하는 요소 2-3개 (예: 주변에 알려주고 싶어지는 공감 포인트)"
  ],
  "comment_cta": {{
    "question": "댓글을 유도하는 마무리 질문 (시청자가 대답하고 싶어지는 것)",
    "why_comments": "왜 이 질문이 댓글을 유도하는지",
    "alternatives": ["댓글 유도 질문 대안 2개"]
  }},
  "cover_frame": {{
    "main_text": "첫 화면 텍스트 (피드에서 클릭하게 만드는 강렬한 문구, 10자 내외)",
    "sub_text": "서브 텍스트 (있다면)",
    "visual": "첫 화면 비주얼 설명 (무엇이 보여야 하는지)",
    "why_clicks": "왜 이 커버가 클릭을 유도하는지"
  }},
  "caption": {{
    "hook_line": "캡션 첫 줄 (더보기 전에 보이는 줄 — 클릭 유도)",
    "body": "캡션 본문 (간결하게, 줄바꿈 포함)",
    "cta": "캡션 마지막 CTA (저장/공유/댓글 유도 문구)",
    "full_caption": "완성된 캡션 전체 (바로 복사해서 쓸 수 있게)"
  }},
  "hashtags": {{
    "core": ["핵심 해시태그 5개 (주제 직결, 검색량 높음)"],
    "niche": ["틈새 해시태그 5개 (경쟁 낮고 타겟 명확)"],
    "trending": ["트렌딩/시즌 해시태그 3개"],
    "strategy": "이 해시태그 조합 전략 설명"
  }},
  "text_overlay_guide": [
    "자막/텍스트 오버레이 스타일 가이드 3-4개 (폰트/위치/강조 방법 등)"
  ],
  "music_mood": "배경음악 분위기 추천 (예: 에너지 넘치는 업비트, 감성적인 인디팝 등) + 이유",
  "loop_tip": "루프 시청을 유도하는 마무리 연출 팁 (끝과 시작이 연결되게 하는 방법)"
}}

hooks는 3개, script는 {duration}초에 맞게 장면을 나눠 작성하세요.
empathy_points는 2-3개를 실제 댓글/카페 데이터에서 인용해 작성하세요. share_triggers의 공감 포인트도 이 실제 데이터와 연결하세요."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip(), msg)

    async def autofill_worksheet(self, keyword: str, ref_videos: List[Dict] = None,
                                 naver: List[Dict] = None, viewtrap_refs: Optional[Dict] = None,
                                 knowledge: List[Dict] = None, brief: str = "") -> Dict:
        """레퍼런스 영상(링크+사용자가 붙여넣은 실제 스크립트)·댓글·썸네일(비전)·카페·ViewTrap을
        분석해 기획 워크시트 칸을 자동 작성. 반환 키는 WS_COLS와 1:1."""
        ref_videos = ref_videos or []

        # 썸네일 이미지(비전 분석용) — thumbnail_url에서 받아옴
        thumb_blocks = []
        for i, v in enumerate(ref_videos, 1):
            b64 = v.get("thumbnail_b64")
            if not b64 and v.get("thumbnail_url"):
                b64 = await self._fetch_thumbnail_b64(v["thumbnail_url"])
            if b64:
                thumb_blocks.append({"type": "text", "text": f"[레퍼런스영상{i} 썸네일: {v.get('title','')[:40]}]"})
                thumb_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})

        # 레퍼런스 영상: 제목·조회수·댓글 + 사용자가 붙여넣은 실제 스크립트
        ref_section = ""
        for i, v in enumerate(ref_videos, 1):
            comments = "\n".join(f"  [{c.get('like_count',0)}좋아요] {c.get('text','')}" for c in v.get("comments", [])[:15])
            script = (v.get("script") or "").strip()
            ref_section += (
                f"\n[레퍼런스영상{i}] {v.get('title','')}\n"
                f"조회수:{v.get('view_count',0):,} / URL:{v.get('url','')}\n"
                f"인기댓글(좋아요순):\n{comments or '  (없음)'}\n"
                + (f"■ 실제 스크립트(도입부·본문 구조 분석의 핵심 근거):\n{script[:5000]}\n" if script else "■ 스크립트 미제공\n")
            )

        market = ""
        if naver:
            market += f"\n== 네이버 카페 반응 ==\n{self._build_naver_text(naver)}\n"
        viewtrap_text = self._build_viewtrap_text(viewtrap_refs)
        if viewtrap_text:
            market += f"\n== ViewTrap 성과영상·핫비디오(검증된 고성과 제목·썸네일 패턴) ==\n{viewtrap_text}\n"

        knowledge_text = self._kb_text(knowledge)

        system_prompt = (
            "당신은 유튜브 콘텐츠 기획 코치입니다. 채널: 부자주방 — 외식업 운영자·자영업·창업 대중 대상. "
            "주차별 실습 시트의 방법론에 따라, 잘된 경쟁영상을 분해하고 그 구조를 내 주제로 '디벨롭'하는 기획 워크시트를 작성합니다. "
            f"{CHANNEL_GOALS} "
            "공감 포인트는 절대 지어내지 말고 실제 유튜브 인기댓글(좋아요순)·네이버 카페 원문에서 인용하세요. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        kt_section = ""
        if knowledge_text:
            kt_section = f"""
== ★★ 사장님 원고·키컨텐츠 작성 강의 — introScript·bodyScript의 '작성 공식'으로 반드시 사용 ★★ ==
{knowledge_text}

[강의 적용 — 반드시 이 순서로]
① 위 강의 규칙 전부 중에서, '이번 영상 도입부·본문에 가장 강력하게 먹힐 규칙 3개'를 먼저 고른다. (막연히 다 적용하려 하지 말고 3개에 집중)
② 그 3개를 introScript·bodyScript에 '확실히, 진하게' 적용한다. 레퍼런스는 훅 각도만 참고하고, 실제 문장은 이 강의 규칙대로 쓴다.
   (도입부 예: 공감[문제제기·유인·사례]→정보 순서, '못난이 정서'로 나를 낮춰 공감, 유인엔 반드시 이유, 감각 공유 / 본문 예: 주장·근거·원리·예시·반론·대안 6요소, 솔직함, 관점 넣기)
③ 어떤 강의의 무슨 규칙을 썼는지 memo 끝에 "적용한 강의 작성법: (강의명) 규칙 — 어디에 적용" 형식으로 명시한다.
[자가검증] introScript를 보고 그 3개 규칙이 문장에 '눈에 띄게' 드러나지 않으면 다시 써라. 강의를 안 본 사람이 쓴 것과 똑같으면 실패다. (단 주제에 안 맞는 규칙을 억지로 넣지는 말 것)
"""

        brief_section = ""
        if brief.strip():
            brief_section = f"""
== ★★ 이번 영상 핵심 내용 (사장님이 직접 적은 '이 영상의 실제 주제·메시지') — 기획의 중심 ★★ ==
{brief.strip()}

[중요] 이 워크시트는 위 '이번 영상 핵심 내용'을 실제로 다루는 영상의 기획입니다. 레퍼런스 영상은 '구조·도입부 틀·심리 기제'를 빌려오는 용도이고, 내용물은 위 핵심 내용으로 채우세요. 제목·도입부·본문 모두 이 핵심 내용이 주인공이 되게 작성하세요.
"""

        user_text = f"""주제/키워드: "{keyword}"
{brief_section}
== 레퍼런스 영상 (사용자가 '카피하기 좋다'고 직접 고른 영상 + 실제 스크립트) ==
{ref_section or "레퍼런스 영상 없음"}
{market or ""}
{kt_section}

위 데이터를 분석해 아래 JSON 형식으로 기획 워크시트를 작성하세요. 각 칸은 '주차별 실습 시트' 예시 형식을 그대로 따르세요.
※ 위에 레퍼런스 영상 썸네일 이미지가 첨부돼 있으면, 그 이미지를 직접 보고 분석하세요.
★가장 먼저 '끝점(고객 언어)'을 설계하세요 — 이 영상 본 시청자가 뒤에서 서로·지인에게 뭐라고 말할지(예: "공감가서 지인한테 소개했잖아", "소탈·진정성 있어서 도와주고 싶어", "배송 늦어도 저 사장님 열심히 하니까 사주고 싶어")를 정하고, 제목·도입부·본문을 '그 반응이 나오도록' 역산해 설계하세요.

[핵심 작업 원칙 — '분석'이 아니라 '디벨롭']
이미 성공한 경쟁영상을 베끼는 게 아니라, 그 영상이 '왜' 먹혔는지 구조를 뽑아 내 주제에 이식합니다. 항상 3단계로 생각하세요:
  (1) 분해 — 잘된 도입부/제목을 beat(마디) 단위로 쪼갠다. (훅 → 긴장/궁금증 조성 → 해결 약속 → 신뢰/전문성)
  (2) 기제 — 각 beat가 시청자 심리에 '왜' 먹히는지 한 문장으로 명시한다. (예: "내가 모르는 손해가 있을까봐 불안 자극")
  (3) 이식 — 그 뼈대(순서·심리 기제)는 유지하고 소재만 내 주제로 갈아끼운다.
실제 스크립트가 있으면 반드시 그 실제 대사를 근거로 (1)(2)를 하세요. 스크립트가 없으면 추정임을 밝히세요.

[칸별 형식 — 반드시 이 형식·톤 준수]
- name: 이 영상 기획의 한 줄 제목(주제)
- thumbA(① 썸네일 분석): 첨부된 레퍼런스 썸네일 이미지를 직접 보고 분석. 정확히 두 줄.
    "-주제 : (썸네일 문구·이미지가 다루는 핵심 주제 — 실제 썸네일에 적힌 문구를 읽어서)"
    "-클릭한 이유 : (썸네일의 문구·구도·표정·색이 시청자의 어떤 고민/궁금증을 건드려 클릭하게 하는지)"
- introA(② 도입부 분석): 경쟁영상 실제 스크립트 앞 30초를 beat 단위로 분해. '- '로 시작하는 5~8줄. 각 줄은 [beat 이름] 실제 대사/장면 요약 → (왜 먹히는지 심리 기제) 형식.
    예: "- [기존상식 뒤집기] '뭘 먹느냐보다 언제 먹느냐' → 당연하게 믿던 걸 부정당해 끝까지 보게 됨". [공감] beat도 표시. 스크립트가 없으면 첫 줄에 "(스크립트 없음 — 댓글·제목 기반 추정)" 명시.
- empathy(공감 포인트): 실제 유튜브 인기댓글/네이버 카페 원문을 인용. 각 항목 끝에 "좋아요 N" 메타 표기. 2~4개. 데이터 없으면 "추정" 표기.
- titleCopy(④ 제목 추천): 제목만 5개. 각 제목은 '선택·후회·조건' 중 하나 이상을 담고, 4가지 변형(①동일주제 ②쉬운단어 ③범위확장 ④강조수식어)을 섞어 다양하게. ViewTrap 검증 제목 패턴이 있으면 카피·변형 반영. 각 줄 끝에 (선택/후회/조건) 표시.
    예: "절대 망하지 않는 식당의 공통점, 딱 이것만 다릅니다 (조건)"
- thumbCopy(④ 섬네일 문구): 섬네일에 박을 문구 3~4개. 제목과 '다른 각도'로(제목+섬네일이 한 세트 시너지). 짧고 강하게(핵심 5~12자), 결과(구체 수치)·감정(놀람/후회/안도)·공감("나도 그래") 중심. 각 줄 끝에 (결과/감정/공감) 표시.
    예: "권리금 3천→1천 (결과)" / "안 깎으면 호구 (감정)"
- thumbDesign(⑤ 섬네일 디자인·촬영 묘사): 사진으로 바로 찍을 수 있게 구체적으로 묘사. 다음을 모두 포함 — 인물 표정·포즈/시선, 텍스트 위치·크기·색(어떤 문구를 어디에), 배경·소품, 색감·분위기, 구도. 레퍼런스 썸네일의 잘된 구조(표정·색·구도)를 참고하되 이 주제 소재로.
    예: "사장님이 계약서를 들고 당황한 표정(클로즈업, 우측). 좌측 상단 굵은 빨강 '권리금 3천?' 노랑 외곽선. 배경은 빈 상가, 채도 낮게. 하단에 작게 '부동산이 안 알려주는 것'."
- introScript(⑥ 도입부 원고): 30초, 구어체, 문장 끊어서. ★작성 방법★ 레퍼런스에서 '먹힌 훅의 각도'만 참고하고, 실제 문장은 위 '원고작성 강의 도입부 공식'으로 쓰세요(공감[문제제기·유인·사례]→정보 순서 등, 강의 원리 1~2개가 실제로 드러나게). ★추가로 반드시★ (1) 섬네일/제목 문구를 30초 안에 '받아주기(callback)' — 그 문구 보고 눌렀으니 도입부에서 짚어줘야 끝까지 봄. (2) 섬네일 기대치를 '넘는' 임팩트(기대 이상의 반전·수치·장면).
- bodyScript(⑦ 본문 원고): 구어체. 위 강의의 본문 작성 원리(예: 주장·근거·원리·예시·반론·대안 6요소, 솔직함, 관점 넣기, 강조 스킬 등)를 적용해 쓰고, empathy의 실제 댓글에서 드러난 시청자 욕구·불안을 직접 녹일 것.
- memo: 참고한 경쟁영상 제목·URL 목록. ViewTrap에서 참고한 성과영상/핫비디오가 있으면 "ViewTrap 참고: 제목(성과율)"도 함께 적기.

{{
  "name": "기획 제목(주제)",
  "keyword": "{keyword}",
  "viewerTalk": "끝점 — 이 영상 본 시청자가 뒤에서 서로/지인에게 나눌 예상 대화 3~4줄 (대화체). 이 반응이 나오게 기획 전체를 설계.",
  "thumbA": "-주제 : ...\\n-클릭한 이유 : ...",
  "introA": "- ...\\n- ...\\n- ...",
  "empathy": "\\"실제 댓글 원문\\" (좋아요 N)\\n\\"실제 카페 글\\" (네이버 카페)",
  "titleCopy": "제목1 (조건)\\n제목2 (후회)\\n제목3 (선택)\\n제목4\\n제목5",
  "thumbCopy": "섬네일문구1 (결과)\\n섬네일문구2 (감정)\\n섬네일문구3 (공감)",
  "thumbDesign": "섬네일 촬영 묘사 (인물 표정·텍스트 위치/색·배경·소품·구도 구체적으로)",
  "introScript": "도입부 대본 (구어체, 섬네일/제목 문구 받아주기 + 기대치 상회)",
  "bodyScript": "본문 대본 (문제→이유→해결→이득)",
  "memo": "참고 영상: 제목 — URL"
}}"""

        content: list = []
        if thumb_blocks:
            content.extend(thumb_blocks)
        content.append({"type": "text", "text": user_text})

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def plan_jjachi(self, topic: str, viewer_heart: str = "", owner_cases: str = "",
                          reality_facts: str = "", filming_env: str = "",
                          videos: List[Dict] = None, naver: List[Dict] = None,
                          knowledge: List[Dict] = None) -> Dict:
        """'짜치는 기획' — 요리 안 하는 주방용품점 사장(수백 주방을 본 조력자) 목소리로,
        원초적 바이럴 인사이트를 적용해 '멋 안 부리고 인간적으로 공감되는' 영상 기획을 만든다."""
        market = ""
        if videos:
            market += f"\n== 유튜브 인기댓글(시청자 진짜 속마음) ==\n{self._build_videos_text(videos, max_videos=5)}\n"
        if naver:
            market += f"\n== 네이버 카페 반응 ==\n{self._build_naver_text(naver)}\n"

        kt = ""
        kb_txt = self._kb_text(knowledge)
        if kb_txt:
            kt = "\n== 사장님 원고·기획 강의(작성 원리) ==\n" + kb_txt + "\n"

        heart = f"\n== 원하는 '시청자의 마음'(끝점) ==\n{viewer_heart.strip()}\n" if viewer_heart.strip() else ""
        cases = f"\n== ★ 운영자가 직접 본 식당 사장님들 사례·하소연 (공감의 원료 — 여기서만 인용) ★ ==\n{owner_cases.strip()}\n" if owner_cases.strip() else ""
        facts = f"\n== ★ 제품·주방 현실 팩트 (반드시 이 사실 범위 안에서만, 없는 디테일 지어내기 금지) ★ ==\n{reality_facts.strip()}\n" if reality_facts.strip() else ""
        env = f"\n== 촬영 가능 환경 ==\n{filming_env.strip()}\n" if filming_env.strip() else ""

        system_prompt = (
            "당신은 '사람의 마음을 얻는 콘텐츠'를 설계하는 유튜브 기획 코치입니다. "
            "채널: 부자주방. ★가장 중요★ 운영자는 요리사·식당 사장이 아니라 '주방용품점 사장'입니다. "
            "장비를 팔고 배달·설치 다니며 수백 개 식당 주방을 직접 봐온 사람이에요. "
            "그래서 절대 '나도 식당하다 데였다'식 1인칭 식당 경험을 지어내지 마세요(=조작, 최악). "
            "진정성은 오직 두 목소리에서 나옵니다: "
            "① 전달자('제가 배달 다니며 본 사장님들은…' = 가십, 원초적 바이럴의 핵심), "
            "② 솔직한 나('저 요리는 못 하는데 주방은 수백 개 봤어요' = 못난이 정서). "
            "공감은 운영자가 아파본 게 아니라, 시청자(식당 사장)가 '어 내 얘기네'라고 느끼도록 "
            "그들의 아픔을 정확히 짚어주고 진심으로 도우려는 태도에서 나옵니다. "
            "모든 사실·장면·디테일은 아래 '제품·주방 현실 팩트'와 '사장님 사례' 범위 안에서만. 없는 건 지어내지 마세요. "
            f"{CHANNEL_GOALS} "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""영상 주제/핵심 내용: "{topic}"
{heart}{cases}{facts}{env}{market}{kt}

[작업 지침]
- 위 '원초적 바이럴' 인사이트를 이 기획의 '작성 공식'으로. 기술 구조 위에 '마음을 움직이는' 층을 얹기.
- ★화자는 '요리 안 하는 주방용품점 사장(수백 주방을 본 조력자)'★. 도입·대본·장면·끝점 전부 이 목소리로. 식당 1인칭 경험 지어내기 절대 금지. 대신 '제가 본 사장님들은…'(전달자) + '저 요리는 못 하는데…'(솔직한 나) 두 화법 사용.
- ★★거울 원칙 (공감 최우선)★★: 감정의 무게중심을 '나(운영자)'가 아니라 **'시청자(식당 사장)의 현실·아픔'**에 둘 것. 목표는 시청자가 "어? 이 사람이 내 주방을 어떻게 알지, 딱 내 얘기네"라고 느끼는 것. 화자 자기 이야기가 주인공이 되면 실패. 시청자의 구체적 상황(그들의 좁은 주방, 그들이 포기한 위칸, 그들이 손님 앞에서 겪은 창피)을 정확히 짚어 '거울처럼' 비추고, 나는 그걸 정확히 이해하고 도와주는 가이드로만 등장. '내 이야기'가 아니라 '당신 이야기'로 시작·전개.
0) ★도입부 첫 문장부터 '시청자 얘기'로★: 시청자의 상황·아픔을 콕 짚어 "딱 내 얘기다" 느끼게 연 다음, 조력자로서 도와주기. (예: "냉장고 맨 위칸, 그냥 포기하고 사시죠?"처럼 시청자를 정면으로)
1) '끝점(고객 언어)' 설계 — 이 영상 본 시청자가 뒤에서 서로/지인에게 뭐라 말할지 대화체로 예상. '시청자의 마음'을 적어줬으면 그게 나오도록. (예: "안 팔려고 하고 진짜 알려주네", "딱 내 주방 얘기라 지인한테 보냈잖아") 이 반응이 나오게 나머지를 역산.
2) '마음을 움직이는 공감 장면'은 **시청자 자신의 현실을 비추는(거울) 장면을 우선**으로 — 운영자가 촬영 가능한 환경(매장/창고/고객 주방 현장)에서 '내가 본 사장님 사례'를 날것으로. 시청자가 자기 주방을 떠올리게.
3) 공감·사례는 절대 지어내지 말고 위 '사장님 사례'·실제 댓글·카페에서만 인용. 제품/현실 디테일은 '팩트' 범위 안에서만(틀린 디테일=마음 닫힘).
4) 목적은 '이 사람 안 팔려고 하고 진짜 도와주네' 느낌 → 응원. 판매는 그 위에 얹기.
5) ★★키신(key scene) 먼저★★: 기획의 출발점은 '이 영상의 단 하나의 키신' — 사람들이 안 누를 수 없고(=썸네일), 다 보고 나서도 기억에 남는 결정적 장면. (흑백요리사=두부 내려오는 장면, 백수저 올라오는 장면처럼) 이 키신을 먼저 정하고, 섬네일 = 그 키신의 한 컷, 나머지 모든 장면을 '그 키신이 터지도록' 배치. 키신 없으면 기획이 아님.
6) ★★촬영 대본★★: 결과물은 '분석 나열'이 아니라 **이대로 들고 가서 찍으면 되는 촬영 대본(콘티)**. 각 장면마다 [어디서 / 누가 / 카메라가 뭘 잡고 / 실제 대사(그대로 읽으면 되게) / 어떤 느낌·연출로 / 자막]. 도입(훅·거울)→공감→조력자(진정성)→진짜 원인→해결·현장 시연→감정 절정(=키신)→끝점 유도 마무리 순서로. 마지막 장면이 '끝점(viewerTalk)' 반응을 만들게.
7) ★이번 주제("{topic}")에만 충실하게★ — 다른 주제(예시로 나온 냉장고 위칸 등)를 절대 끌어오지 말 것. 아래 예시 문구는 형식 참고용일 뿐, 반드시 이번 주제 상황으로 새로 쓸 것.

아래 JSON으로 작성하세요 (keyScene·scenes가 핵심):
{{
  "logline": "이 영상 한 줄 컨셉 — 어디서 / 누구와 / 무엇을 보여주는지 (예: '진짜 고객 식당 주방에서, 매일 부딪히는 사장님과 함께, ○○ 하나 바꿔 달라지는 걸 눈앞에서')",
  "keyScene": {{"scene":"★키신★ 이 영상 단 하나의 안 누를 수 없는·기억에 남는 결정적 장면 (구체적으로, 이번 주제로)","why":"왜 이게 키신인지 — 왜 클릭을 부르고 마음을 움직이는지","sceneNo":"아래 scenes 중 이 키신에 해당하는 장면 번호"}},
  "viewerMirror": "★거울★ 이 영상이 정확히 비출 시청자(식당 사장)의 구체적 현실·아픔 — '딱 내 얘기네' 지점 (그들 입장에서 한 문단, 이번 주제에 맞게)",
  "coreEmotion": "시청자가 느낄 핵심 감정 한 문장 (시청자 입장)",
  "viewerTalk": ["★끝점★ 이 영상 본 시청자가 뒤에서 서로/지인에게 나눌 예상 대화 3~4개 (대화체). 마지막 장면이 이 반응을 만들어야 함."],
  "scenes": [
    {{
      "no": 1,
      "beat": "이 장면 역할 (예: 훅/거울, 공감, 진정성, 문제, 해결·시연, 절정, 마무리·끝점)",
      "seconds": "대략 길이 (예: ~15초)",
      "location": "어디서 촬영 (예: 고객 식당 주방 / 내 매장·창고)",
      "cast": "누가 나오는지 (예: 나(주방용품점 사장) / 진짜 고객 사장님)",
      "visual": "카메라가 무엇을 잡는지 — 구체적 그림",
      "dialogue": "실제 대사 — 그대로 읽으면 되는 구어체 대본 (없으면 빈 문자열)",
      "direction": "느낌·연출 — 톤, 표정, 페이스, 편집 포인트",
      "caption": "화면 자막 (있으면, 없으면 빈 문자열)"
    }}
  ],
  "titles": ["마음 건드리는 제목 3~5개 (이번 주제로)"],
  "thumbnail": "섬네일 = 위 '키신'의 한 컷. 문구 + 구도 한 줄 (이번 주제, 짜치지만 안 누를 수 없게)",
  "note": "이 대본이 왜 '짜치면서도 마음을 울리고 바이럴 되는지' 1~2줄 (거울·진정성·전염 포인트)"
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_edit_feedback(self, keyword: str, script: str, videos: List[Dict], naver: List[Dict], viewtrap_refs: Optional[Dict] = None) -> Dict:
        videos_text = self._build_videos_text(videos, max_videos=10, max_comments=30)
        naver_text = self._build_naver_text(naver)
        viewtrap_text = self._build_viewtrap_text(viewtrap_refs)

        system_prompt = (
            "당신은 유튜브 영상 편집 전략 전문가입니다. "
            "시장 데이터(경쟁 영상 댓글, 시청자 반응)를 기반으로 제공된 영상 대본을 분석하여 "
            "편집 방향에 대한 구체적이고 실행 가능한 피드백을 제공합니다. "
            f"{CHANNEL_GOALS}"
            f"{CONTENT_CREATION_RULES}"
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""키워드: "{keyword}"

== 시장 데이터: 유튜브 상위 영상 ==
{videos_text}

== 시장 데이터: 네이버 카페 반응 ==
{naver_text}
{f'''
== ViewTrap 성과/핫비디오 레퍼런스 ==
{viewtrap_text}

[ViewTrap 활용 지침]
- 성과 영상·핫비디오 중 이 키워드("{keyword}")와 주제가 유사하거나 같은 카테고리의 영상을 찾아 분석하세요
- 해당 레퍼런스 영상의 제목 패턴·썸네일 구성·영상 구조에서 잘 된 점을 파악하고
  내 대본이 그 패턴을 따르고 있는지, 어떻게 개선하면 유사한 성과를 낼 수 있는지 구체적으로 제시하세요
- viewtrap_insights 필드에 어떤 레퍼런스 영상을 참고했고 어떻게 적용할 수 있는지 명시하세요
''' if viewtrap_text else ''}

== 내 영상 대본 ==
{script}

위 시장 데이터를 기반으로 내 영상 대본을 분석하여 아래 JSON 형식으로 편집 피드백을 작성하세요.
대본의 실제 내용을 구체적으로 언급하며 피드백하세요 (추상적 표현 금지).

{{
  "overall_assessment": "전반적인 영상 평가 및 시장 적합도 2-3문장",
  "market_fit_score": 시장적합도점수(0-100사이정수),
  "strengths": ["잘 된 점 3-5개 (대본의 구체적 내용 언급)"],
  "keep_sections": [
    {{
      "section": "살려야 할 구간 (대본에서 구체적으로 어떤 내용인지)",
      "reason": "왜 살려야 하는지 (시청자 욕구와 연결)",
      "priority": "high 또는 medium"
    }}
  ],
  "cut_sections": [
    {{
      "section": "삭제 추천 구간 (대본에서 구체적으로 어떤 내용인지)",
      "reason": "왜 삭제해야 하는지",
      "alternative": "대신 넣으면 좋은 내용"
    }}
  ],
  "hook_feedback": "인트로 첫 30초 피드백: ① 썸네일 기대를 넘는 임팩트가 있는지 ② 썸네일 문구를 도입부에서 다시 언급했는지 ③ 공감 장면이 앞에 있는지 ④ 임팩트 있는 장면을 앞으로 당겼는지 — 각각 평가하고 개선안 제시",
  "intro_improvement": {{
    "thumbnail_callback_exists": "썸네일 언급 내용을 도입부에서 반복했는지 (있음/없음)",
    "thumbnail_callback_suggestion": "없다면: 어떤 문구를 도입부 어디에 넣어야 하는지",
    "impact_scene": "앞으로 당겨야 할 임팩트 장면 (구체적으로 대본 어느 부분)",
    "empathy_check": "공감 요소가 충분한지, 부족하다면 어떤 공감 포인트를 추가해야 하는지"
  }},
  "title_feedback": {{
    "current_analysis": "현재 제목/키워드의 선택·후회·조건 요소 포함 여부 분석",
    "improved_titles": [
      {{
        "title": "개선된 제목 (선택·후회·조건 중 하나 포함)",
        "title_keyword_type": "선택 또는 후회 또는 조건",
        "hook_reason": "왜 클릭하고 싶어지는지"
      }}
    ]
  }},
  "edit_flow_suggestions": ["편집 흐름/순서 개선점 4-6개 (욕구 자극 관점에서 구체적으로)"],
  "script_desire_feedback": "원고가 시청자의 욕구를 충분히 불러일으키는지 평가. 물건 소개라면 사고 싶은 충동이 드는지 — 부족한 부분과 개선 방향 제시",
  "missing_content": ["시청자가 원하는데 대본에 없는 내용 4-6개"],
  "recommended_titles": [
    {{
      "title": "클릭률 최적화 제목 (선택·후회·조건 포함)",
      "title_keyword_type": "선택 또는 후회 또는 조건",
      "hook_reason": "왜 클릭하고 싶어지는지",
      "target_emotion": "유발하는 감정"
    }}
  ],
  "thumbnail_recommendations": [
    {{
      "concept": "썸네일 전체 컨셉",
      "main_text": "썸네일 메인 텍스트 (결과·감정·공감 중심, 짧고 강렬하게)",
      "emotion_type": "결과 또는 감정 또는 공감",
      "visual_element": "이미지/배경 요소 설명",
      "reason": "왜 이 썸네일이 클릭을 유도하는지"
    }}
  ],
  "viewtrap_insights": {{
    "referenced_videos": ["참고한 ViewTrap 영상 제목 목록 (없으면 빈 배열)"],
    "applied_patterns": "레퍼런스 영상의 어떤 패턴을 내 영상에 어떻게 적용하면 좋은지 구체적 설명 (ViewTrap 데이터가 없으면 빈 문자열)"
  }}
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        if msg.stop_reason == "max_tokens":
            raise ValueError("AI 응답이 너무 길어 잘렸습니다. 대본을 조금 줄이고 다시 시도해주세요.")

        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_channel(self, channel_info: Dict, videos: List[Dict]) -> Dict:
        from collections import defaultdict

        top10 = sorted(videos, key=lambda x: x["view_count"], reverse=True)[:10]
        recent50 = videos[:30]

        def _fmt_video(v, idx):
            ctr = f" | CTR:{v['ctr']}%" if v.get("ctr") is not None else ""
            ret = f" | 유지율:{v['avg_view_percentage']}%" if v.get("avg_view_percentage") is not None else ""
            watch = f" | 시청{v['watch_minutes']:,}분" if v.get("watch_minutes") is not None else ""
            return (
                f"[{idx}] {v['title']} | 조회수:{v['view_count']:,}{ctr}{ret}{watch}"
                f" | {v['published_at']} ({v['publish_day']}요일) | {v['duration_sec']//60}분"
            )

        top10_text = "\n".join(_fmt_video(v, i+1) for i, v in enumerate(top10))

        bottom10 = sorted(videos, key=lambda x: x["view_count"])[:10]
        bottom10_text = "\n".join(_fmt_video(v, i+1) for i, v in enumerate(bottom10))

        recent_text = "\n".join(
            f"{v['published_at']} ({v['publish_day']}요일) | {v['title']} | 조회수:{v['view_count']:,} | {v['duration_sec']//60}분"
            for v in recent50
        )

        day_stats: dict = defaultdict(list)
        hour_stats: dict = defaultdict(list)
        for v in videos:
            if v["publish_day"]:
                day_stats[v["publish_day"]].append(v["view_count"])
            if v["publish_hour"] is not None:
                bucket = f"{v['publish_hour']:02d}시"
                hour_stats[bucket].append(v["view_count"])

        day_avg_text = " / ".join(
            f"{day}요일 평균{int(sum(vws)/len(vws)):,}회({len(vws)}개)"
            for day, vws in sorted(day_stats.items(), key=lambda x: -sum(x[1])/len(x[1]))
        )
        hour_avg_text = " / ".join(
            f"{hour} 평균{int(sum(vws)/len(vws)):,}회"
            for hour, vws in sorted(hour_stats.items(), key=lambda x: -sum(x[1])/len(x[1]))[:6]
        )

        dur_buckets: dict = defaultdict(list)
        for v in videos:
            d = v["duration_sec"]
            if d < 180:
                bucket = "3분 미만"
            elif d < 480:
                bucket = "3-8분"
            elif d < 900:
                bucket = "8-15분"
            elif d < 1800:
                bucket = "15-30분"
            else:
                bucket = "30분 이상"
            dur_buckets[bucket].append(v["view_count"])
        dur_text = " / ".join(
            f"{b}: 평균{int(sum(vws)/len(vws)):,}회({len(vws)}개)"
            for b, vws in sorted(dur_buckets.items(), key=lambda x: -sum(x[1])/len(x[1]))
        )

        prompt = f"""채널 분석 데이터:
채널명: {channel_info['title']}
구독자: {channel_info['subscriber_count']:,}명 / 총 영상: {channel_info['video_count']}개 / 총 조회수: {channel_info['view_count']:,}회
분석 대상: {len(videos)}개 영상

== 조회수 TOP 10 ==
{top10_text}

== 조회수 하위 10개 ==
{bottom10_text}

== 최근 50개 업로드 현황 ==
{recent_text}

== 요일별 평균 조회수 ==
{day_avg_text}

== 시간대별 평균 조회수 (상위 6개) ==
{hour_avg_text}

== 영상 길이별 평균 조회수 ==
{dur_text}

위 데이터를 분석하여 아래 JSON으로 한국어 리포트를 작성하세요. 추상적 표현 금지, 데이터 기반으로 구체적으로.

{{
  "channel_summary": "채널 현황 종합 분석 3-4문장 (구독자 대비 조회수 수준, 강점, 핵심 문제)",
  "top_performing_topics": [
    {{"topic": "잘되는 주제/카테고리명", "reason": "왜 잘되는지 근거", "avg_views": 숫자, "example": "예시 영상 제목"}}
  ],
  "underperforming_topics": [
    {{"topic": "안되는 주제/카테고리명", "reason": "왜 안되는지 근거", "avg_views": 숫자}}
  ],
  "best_upload_days": ["최적 요일 1위 (요일+이유)", "2위"],
  "worst_upload_days": ["피해야 할 요일 (이유 포함)"],
  "best_upload_hours": ["최적 시간대 (예: 오후 6-7시 — 이유)"],
  "optimal_video_length": "최적 영상 길이와 이유 (데이터 기반)",
  "successful_title_patterns": [
    {{"pattern": "제목 패턴명", "example": "예시 제목", "why": "왜 효과적인지"}}
  ],
  "growth_bottleneck": "채널 성장의 핵심 병목 (데이터 기반, 구체적으로)",
  "channel_recommendations": [
    "지금 당장 실행 가능한 개선 방안 (구체적으로, 5-7개)"
  ],
  "next_video_strategy": "다음 영상에서 반드시 적용할 전략 3가지 (데이터 기반)"
}}

top_performing_topics 5개, underperforming_topics 3개, successful_title_patterns 4개 작성."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=f"당신은 유튜브 채널 성장 전략 전문가입니다. 데이터 기반으로 구체적인 인사이트를 제공합니다. {CHANNEL_GOALS} 반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만.",
            messages=[{"role": "user", "content": prompt}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def chat_stream(self, message: str, history: List[Dict], attachments: List[Dict] = None):
        messages = [{"role": m["role"], "content": m["content"]} for m in history[-20:]]

        # 첨부파일이 있으면 content를 리스트(멀티모달)로 구성
        if attachments:
            content: list = []
            for att in attachments:
                mt = att.get("media_type", "")
                if mt.startswith("image/"):
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mt, "data": att["data"]},
                    })
                elif mt == "application/pdf":
                    content.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": mt, "data": att["data"]},
                    })
            if message.strip():
                content.append({"type": "text", "text": message})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": message})

        async with self.client.messages.stream(
            model=WRITER_MODEL,
            max_tokens=4096,
            system=CHAT_SYSTEM,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def analyze_video_decision(self, videos_info: List[Dict], current_date: str) -> Dict:
        videos_text = "\n\n".join(
            f"[영상 {i+1}]\n"
            f"제목 아이디어: {v.get('title', '없음')}\n"
            f"내용 설명: {v.get('description', '없음')}\n"
            f"대본/아웃라인: {(v.get('script') or '없음')[:600]}\n"
            f"썸네일 컨셉: {v.get('thumbnail_concept') or '없음'}"
            for i, v in enumerate(videos_info)
        )

        prompt = f"""오늘 날짜: {current_date}

아래는 촬영 완료된 영상들입니다. 어떤 순서로, 언제 올리면 가장 좋을지 분석해주세요.

{videos_text}

분석 기준:
1. 현재 시기 트렌드/계절성 (오늘 날짜 기준)
2. 조회수 가능성 (제목 클릭률 + 내용 시청 유지율 예측)
3. 경쟁 콘텐츠와의 차별화
4. 업로드 간격과 채널 알고리즘 최적화

아래 JSON으로 답변하세요:

{{
  "ranking": [
    {{
      "rank": 1,
      "video_index": 1,
      "original_title": "원래 제목 아이디어",
      "performance_score": 숫자(0-100),
      "reason": "이 영상을 이 순위로 추천하는 이유 (구체적으로 3-4문장)",
      "timing_recommendation": "구체적 업로드 일정 (예: 이번 주 화요일 오후 6시)",
      "improved_title": "개선된 제목 제안 (CTR 높이는 버전)",
      "thumbnail_tip": "썸네일 핵심 개선 포인트",
      "risk": "이 영상의 잠재적 리스크 또는 주의할 점"
    }}
  ],
  "upload_schedule": "전체 업로드 스케줄 제안 (모든 영상 포함, 날짜/요일/시간 구체적으로)",
  "overall_strategy": "이 영상들을 올릴 때의 전체 전략 (2-3문장)"
}}

ranking 배열에 입력받은 영상 전체를 순위 매겨 포함하세요."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=f"당신은 유튜브 크리에이터 전략 컨설턴트입니다. 데이터와 트렌드 기반으로 구체적인 업로드 전략을 제시합니다. {CHANNEL_GOALS} 반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만.",
            messages=[{"role": "user", "content": prompt}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_sns_convert(self, keyword: str, script: str) -> Dict:
        system_prompt = (
            "당신은 SNS 콘텐츠 마케팅 전문가입니다. "
            "부자주방 채널의 유튜브 대본이나 콘텐츠를 블로그, 스레드, 숏폼 스크립트로 변환합니다. "
            "채널: 부자주방 — 외식업 운영자(식당·분식집·한식당 사장님) 대상 업소용 주방용품 전문 채널. "
            "각 플랫폼의 특성에 맞는 말투와 구조로 변환하세요. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""키워드(주제): "{keyword}"

== 원본 대본/내용 ==
{script}

위 내용을 아래 3가지 SNS 형식으로 변환하세요.

[블로그 포스팅 규칙]
- 블로그 말투 (~했어요, ~이에요, ~해보세요 등 친근한 경어체)
- "{keyword}" 키워드를 본문에 최소 10회 이상 자연스럽게 반복
- 구조: 제목 → 도입부(공감) → 본문(3-5 소제목) → 마무리(요약+CTA)
- 2000자 내외, 읽기 좋은 단락 구성
- SEO용 첫 문단에 키워드 2회 포함
- 맨 앞줄: #{keyword} (해시태그 형식)

[스레드 포스트 규칙]
- 5~7개의 연결된 짧은 포스트
- 각 포스트는 200자 이내
- 첫 포스트: 강렬한 훅 (스크롤 멈추게)
- 중간: 핵심 정보를 번호나 이모지로 정리
- 마지막: 공유/댓글 유도 질문
- 스레드 특유의 짧고 끊기는 문체

[숏폼 스크립트 규칙]
- 총 45-60초 분량
- 훅(0-3초): 스크롤 멈추는 강렬한 첫 문장
- 본문(4-45초): 핵심 포인트 3가지, 빠른 템포
- CTA(마지막 5초): 저장/팔로우/댓글 유도
- 인스타그램 릴스·유튜브 쇼츠·틱톡 공통 사용 가능
- 실제 말하는 구어체

{{
  "blog": {{
    "title": "블로그 포스팅 제목 (검색 최적화, 30자 내외)",
    "meta_description": "검색 결과에 노출될 요약문 (150자 내외, 키워드 포함)",
    "content": "완성된 블로그 포스팅 전체 (맨 앞에 #{keyword} 해시태그, 키워드 10회+ 반복, 소제목은 ## 사용, 2000자 내외)",
    "keyword_count_note": "키워드가 몇 번 사용됐는지",
    "seo_tags": ["SEO 태그 5-7개"]
  }},
  "threads": {{
    "posts": [
      {{
        "order": 1,
        "content": "첫 번째 포스트 내용 (훅)",
        "type": "hook"
      }}
    ],
    "total_posts": 포스트수
  }},
  "shortform": {{
    "hook": "0-3초 훅 문장",
    "hook_type": "훅 유형 (충격/공감/질문/비밀공개 중 하나)",
    "body_points": [
      {{
        "time": "00:04-00:20",
        "narration": "실제 말할 내용 (구어체)",
        "text_overlay": "화면에 띄울 텍스트 (짧고 굵게)"
      }}
    ],
    "cta": "마지막 5초 CTA 대사",
    "total_seconds": 예상초,
    "platforms": ["인스타그램 릴스", "유튜브 쇼츠", "틱톡"]
  }}
}}

threads.posts 배열에 5-7개 포스트를 작성하세요. body_points는 3개 작성하세요."""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_detail_page(self, keyword: str, product_desc: str, price: str,
                                   target_customer: str, videos: List[Dict], naver: List[Dict]) -> Dict:
        videos_text = self._build_videos_text(videos, max_videos=10, max_comments=30)
        naver_text = self._build_naver_text(naver)

        system_prompt = (
            "당신은 쇼핑몰 상세페이지 전략 전문가입니다. "
            "유튜브 영상 기획과 동일한 원리로 — 도입부 후킹 → 공감 → 문제 심화 → 솔루션 제시 → 신뢰 구축 → 구매 유도 — "
            "고객이 구매 버튼을 누르게 만드는 상세페이지를 설계합니다. "
            "시장 데이터(유사 제품 리뷰·댓글)에서 고객의 실제 욕구와 불만을 파악하여 반영하세요. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""제품 키워드: "{keyword}"
제품 설명: {product_desc}
가격대: {price or '미기재'}
타겟 고객: {target_customer or '미기재'}

== 시장 데이터: 유사 제품 유튜브 리뷰·반응 ==
{videos_text}

== 시장 데이터: 네이버 카페·커뮤니티 반응 ==
{naver_text}

위 시장 데이터를 기반으로 이 제품의 쇼핑몰 상세페이지 기획안을 작성하세요.
유튜브 영상 기획과 동일한 구조(후킹→공감→문제→솔루션→신뢰→구매유도)를 상세페이지에 적용하세요.
잘 팔리는 유사 제품의 패턴을 분석해 인용하세요.
모든 카피는 실제 바로 쓸 수 있을 정도로 구체적으로 작성하세요.

{{
  "market_summary": "유사 제품 시장 분석 요약 2-3문장 (고객이 무엇을 원하고 무엇에 지쳐있는지)",
  "customer_pain_points": ["고객의 핵심 페인포인트 6-8개 (실제 댓글/리뷰 데이터 기반, 구체적으로)"],
  "purchase_triggers": ["구매 결정 요인 5-7개 (이 제품 카테고리에서 실제로 구매를 결정하게 만드는 요소)"],
  "competitor_patterns": ["잘 팔리는 유사 제품 상세페이지에서 공통적으로 나타나는 패턴 5-7개"],
  "page_sections": [
    {{
      "order": 1,
      "section_name": "섹션 이름 (예: 도입부 후킹)",
      "purpose": "이 섹션의 역할 한 줄",
      "headline": "이 섹션의 헤드라인 카피 (실제 사용 가능하게)",
      "body_copy": "본문 카피 (2-4문장, 실제 사용 가능하게)",
      "visual_suggestion": "이미지/영상 제안 (구체적으로)",
      "hook_technique": "여기서 쓰는 설득 기법 (공감/문제제시/증거/희소성 등)"
    }}
  ],
  "key_copies": {{
    "main_headline": "메인 헤드라인 (페이지 상단 첫 문장, 스크롤을 멈추게 만드는)",
    "sub_headline": "서브 헤드라인 (메인 아래, 구체적 혜택 또는 공감)",
    "empathy_opener": "공감 오프너 (고객이 '맞아 내 얘기다' 하는 첫 문장)",
    "problem_agitation": ["문제 심화 문구 4-5개 (고통을 더 선명하게 만드는)"],
    "solution_reveal": "솔루션 등장 문구 (반전 느낌으로)",
    "core_benefits": ["핵심 베네핏 5-7개 (기능 말고 결과·감정 중심으로)"],
    "cta_options": [
      {{
        "text": "CTA 버튼 문구",
        "urgency_element": "긴급성/희소성 요소",
        "reason": "왜 지금 사야 하는지"
      }}
    ]
  }},
  "trust_building": {{
    "review_keywords": ["리뷰에서 강조해야 할 키워드 5-7개"],
    "certification_suggestions": ["신뢰도 높이는 인증·보증 요소 3-5개"],
    "before_after": "Before/After 구성 제안 (어떤 변화를 보여줄지)"
  }},
  "differentiation": ["경쟁 제품 대비 차별화 포인트 4-6개 (근거 포함)"],
  "recommended_titles": [
    {{
      "title": "상세페이지 상단 메인 타이틀 (검색 노출 + 클릭 최적화)",
      "hook_reason": "왜 클릭·구매 욕구를 자극하는지"
    }}
  ]
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_blog(self, keyword: str, memo: str, photos: list = None, region: str = "", link: str = "") -> Dict:
        photos = photos or []
        photo_count = len(photos)

        system_prompt = (
            "당신은 네이버 블로그 SEO 전문가이자 부자주방 브랜드 콘텐츠 전문가입니다. "
            "사진이 첨부된 경우 각 사진에 실제로 있는 것만 묘사하세요. "
            "'이 사진은', '위 사진처럼' 등 메타 표현은 절대 사용하지 마세요. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        # 사진 배분 계산
        def plan_sections(n):
            if n == 0:
                # 사진 없을 때 텍스트 전용 섹션
                return [
                    {"type": "intro", "heading": "도입부", "photos": []},
                    {"type": "subtitle", "heading": "소제목1", "photos": []},
                    {"type": "subtitle", "heading": "소제목2", "photos": []},
                    {"type": "subtitle", "heading": "소제목3", "photos": []},
                    {"type": "subtitle", "heading": "소제목4", "photos": []},
                    {"type": "closing", "heading": "마무리", "photos": []},
                ]
            idx = 0
            sections = []
            remaining = n

            # 도입부: 1~2장
            intro_n = min(2, remaining)
            intro_photos = list(range(idx, idx + intro_n))
            sections.append({"type": "intro", "heading": "도입부", "photos": intro_photos})
            idx += intro_n; remaining -= intro_n

            # 소제목들: 최소 2개
            sub_allocs = []
            if remaining >= 7:
                sub_allocs = [min(3, remaining // 3), min(3, remaining // 3), 2, 2]
            elif remaining >= 5:
                sub_allocs = [min(3, remaining - 3), 2, 2]
            elif remaining >= 3:
                sub_allocs = [min(3, remaining - 1), 1]
            elif remaining >= 2:
                sub_allocs = [1, 1]
            elif remaining == 1:
                sub_allocs = [1]
            else:
                sub_allocs = []

            for si, alloc in enumerate(sub_allocs, 1):
                ph = list(range(idx, idx + alloc))
                sections.append({"type": "subtitle", "heading": f"소제목{si}", "photos": ph})
                idx += alloc; remaining -= alloc

            # 마무리: 남은 장 (최대 1장)
            closing_n = min(1, remaining)
            closing_photos = list(range(idx, idx + closing_n))
            sections.append({"type": "closing", "heading": "마무리", "photos": closing_photos})

            return sections

        sections_plan = plan_sections(photo_count)

        # 섹션 배분 설명 텍스트
        section_plan_text = ""
        for s in sections_plan:
            ph_str = f"사진 {[p+1 for p in s['photos']]}" if s['photos'] else "사진 없음"
            section_plan_text += f"- {s['heading']}: {ph_str}\n"

        memo_text   = f"\n업체 소개: {memo}" if memo.strip() else ""
        region_text = f"\n지역: {region}" if region.strip() else ""
        link_text   = f"\n참고 링크: {link}" if link.strip() else ""

        user_text = f"""브랜드: 부자주방 (업소용 주방기기 판매)
타겟: 식당 자영업자
말투: 친근한 존댓말
키워드: {keyword}{memo_text}{region_text}{link_text}

다음 사진들을 분석하여 네이버 블로그용 SEO 최적화 글을 작성해주세요.

[사진 배분]
{section_plan_text}

[글 구조 요구사항]
- 도입부 body: 250자 이상
- 소제목 각 body: 350자 이상
- 마무리 body: 250자 이상 (반드시 연락처+자사몰 CTA 포함)
- 전체 공백제외 2800자 이상

[키워드 전략]
- 메인 키워드 ({keyword}) 전체 반드시 8~10회 포함 (부족하면 재작성)
- 연관 키워드 2~4회
- LSI 키워드 (제품 스펙, 업종명) 자연스럽게
- 메인 키워드 첫 문장 필수 포함
- 소제목 제목에 메인 키워드 포함
- 키워드 억지 나열 절대 금지

[연락처·자사몰 CTA 전략 — 필수]
아래 두 가지를 합산 5회 이상 자연스럽게 본문에 녹여 넣으세요.
- 전화 문의: 1600-6787 (예: "궁금한 점은 1600-6787로 문의주세요", "부자주방 1600-6787")
- 자사몰: www.bujaikm.com (예: "www.bujaikm.com 에서 시공사례를 확인해보세요", "www.bujaikm.com 간편문의")
각 섹션에 분산 배치하고, 마무리에 반드시 둘 다 포함.

[소제목2 필수]
비교표나 리스트 반드시 포함 (마크다운 표 형식)

[사진 작성 원칙 — 중요]
- 각 섹션은 전체 글의 주제와 맥락에 맞는 본문(body)을 먼저 충분히 작성
- 사진은 본문의 주인공이 아니라 보조 자료 — photo_captions에 한 줄씩만 부가 설명
- "이 사진은", "위 사진처럼", "아래 사진에서" 등 메타 표현 절대 금지
- 사진에 실제로 있는 것만 묘사, 자연스러운 서술형

다음 JSON으로 출력해주세요:
{{
  "titles": ["제목후보1 (키워드 앞15자이내, 25-35자, 숫자포함)", "제목후보2", "제목후보3"],
  "sections": [
    {{
      "type": "intro",
      "heading": "도입부",
      "body": "전체 주제에 맞는 도입 본문 (키워드+CTA 포함)...",
      "photo_captions": [
        {{"photo_index": 0, "caption": "한 줄 사진 부가설명"}},
        {{"photo_index": 1, "caption": "한 줄 사진 부가설명"}}
      ]
    }},
    {{
      "type": "subtitle",
      "heading": "소제목1 (키워드 포함)",
      "body": "전체 주제에 맞는 본문 (키워드+CTA 포함)...",
      "photo_captions": [
        {{"photo_index": 2, "caption": "한 줄 사진 부가설명"}},
        {{"photo_index": 3, "caption": "한 줄 사진 부가설명"}}
      ]
    }},
    {{
      "type": "closing",
      "heading": "마무리",
      "body": "... 1600-6787로 문의주세요. www.bujaikm.com 에서 시공사례와 간편문의를 남겨주세요.",
      "photo_captions": [
        {{"photo_index": 12, "caption": "한 줄 사진 부가설명"}}
      ]
    }}
  ],
  "filenames": ["업소용냉장고-추천-01.jpg", "업소용냉장고-추천-02.jpg"],
  "tags": ["태그1", "태그2"],
  "checklist": {{
    "quality": ["복붙 금지", "외부 링크 3개 초과 금지", "키워드 나열 금지", "하루 1개 이상 포스팅 금지", "다른 블로그 사진 무단 사용 금지"],
    "publish_guide": {{
      "best_time": "오전 7~9시 또는 오후 12~1시",
      "frequency": "주 2~3회 꾸준히",
      "interval": "같은 주제는 2주 간격",
      "optimization": "발행 후 2~3일 내 반응 보고 제목·첫 문단 수정 가능"
    }}
  }}
}}

sections 배열은 위 사진 배분 계획대로 작성하세요.
photo_captions는 해당 섹션에 배분된 사진 수만큼 작성하세요. (사진 없는 섹션은 빈 배열)
filenames는 사진 총 {photo_count}개에 맞춰 작성하세요. (사진 없으면 빈 배열)
tags는 정확히 30개 작성하세요."""

        # 멀티모달 content 구성
        if photos:
            content: list = []
            for i, p in enumerate(photos):
                content.append({"type": "text", "text": f"[사진 {i+1}]"})
                content.append({"type": "image", "source": {
                    "type": "base64",
                    "media_type": p.get("media_type", "image/jpeg"),
                    "data": p["data"]
                }})
            content.append({"type": "text", "text": user_text})
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": user_text}]

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=messages,
        )
        return _safe_json(msg.content[0].text.strip(), msg)

    async def analyze_video_feedback(self, transcript: str) -> dict:
        system_prompt = (
            "당신은 유튜브 영상 퍼포먼스 전문 분석가입니다. "
            "부자주방 채널(업소용 주방용품 전문 유튜버)의 영상을 분석합니다. "
            "채널 목표: CTR 10% 이상, 초반 30초 이탈률 40% 미만. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""아래는 유튜브 영상의 자막입니다. 부자주방 채널 (주방용품 전문 유튜버)의 영상입니다.
목표: CTR 10% 이상, 초반 30초 이탈률 40% 미만

[자막]
{transcript}

다음 항목을 JSON으로 분석해주세요:
{{
  "overall_score": 85,
  "hook_analysis": {{
    "score": 90,
    "first_30s": "초반 30초 내용 요약",
    "hook_strength": "강함/보통/약함",
    "improvement": "개선 제안"
  }},
  "content_flow": {{
    "score": 80,
    "summary": "전체 내용 흐름 요약 (3줄)",
    "key_message": "핵심 메시지",
    "pacing": "빠름/적절/느림"
  }},
  "ctr_prediction": {{
    "score": 75,
    "analysis": "CTR 예측 근거",
    "title_suggestion": ["추천 제목 1", "추천 제목 2", "추천 제목 3"]
  }},
  "retention_risk": {{
    "score": 70,
    "weak_points": ["이탈 위험 구간 1", "이탈 위험 구간 2"],
    "suggestion": "시청 유지율 개선 방안"
  }},
  "strengths": ["잘된 점 1", "잘된 점 2"],
  "improvements": ["개선할 점 1", "개선할 점 2", "개선할 점 3"]
}}"""

        msg = await self.client.messages.create(
            model=WRITER_MODEL,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        return _safe_json(msg.content[0].text.strip(), msg)
