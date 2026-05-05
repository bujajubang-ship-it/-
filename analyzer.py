import json
import re
import base64
import httpx
import anthropic
from typing import List, Dict, Optional


def _safe_json(raw: str) -> dict:
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
            return json.loads(m.group())
        raise ValueError(f"JSON 파싱 실패: {raw[:200]}")


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

    def _build_videos_text(self, videos: List[Dict]) -> str:
        parts = []
        for i, v in enumerate(videos[:10], 1):
            comments_block = "\n".join(
                f"  [{c['like_count']}좋아요] {c['text']}"
                for c in v.get("comments", [])[:30]
            )
            parts.append(
                f"[영상{i}] {v['title']}\n"
                f"조회수:{v['view_count']:,} / 좋아요:{v['like_count']:,} / 댓글:{v['comment_count']:,}\n"
                f"채널:{v['channel']} / 업로드:{v['published_at']}\n"
                f"URL:{v['url']}\n"
                f"설명:{v['description'][:300]}\n"
                f"인기댓글(좋아요순):\n{comments_block or '  (댓글 없음)'}\n"
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
  ]
}}"""

        content: list = []
        if thumb_blocks:
            content.append({"type": "text", "text": "상위 영상 썸네일 (썸네일 스타일 분석에 반영):\n"})
            content.extend(thumb_blocks)
        content.append({"type": "text", "text": user_text})

        msg = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
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
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip())

    async def write_intro(self, keyword: str, product_desc: str, problem_definition: str, viewer_desire: str) -> Dict:
        system_prompt = (
            "당신은 유튜브 영상 도입부 전문 작가입니다. "
            "문제제기 → 공감 → 손해 → 이득 → 사례 공식으로 시청자를 30초 안에 사로잡는 도입부를 작성합니다. "
            "실제로 카메라 앞에서 말할 수 있는 자연스러운 구어체 한국어로 작성하세요. "
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
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip())

    async def write_script(self, keyword: str, product_desc: str, reference_script: str, context: str) -> Dict:
        system_prompt = (
            "당신은 유튜브 영상 대본 작가입니다. "
            "잘 된 영상 대본의 구조와 흐름을 분석하고, 내 제품/주제에 맞게 변형하여 "
            "시청자가 더 좋아할 수 있도록 책/전문가/시연 요소를 추가하고 댓글 유도로 마무리합니다. "
            "실제로 촬영할 수 있는 구어체 한국어로 작성하세요. "
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
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip())

    async def analyze_midform(self, keyword: str, product_desc: str, videos: List[Dict], naver: List[Dict]) -> Dict:
        videos_text = self._build_videos_text(videos)
        naver_text = self._build_naver_text(naver)

        thumb_blocks = []
        for v in videos[:3]:
            url = v.get("thumbnail_url", "")
            if url:
                b64 = await self._fetch_thumbnail_b64(url)
                if b64:
                    thumb_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})

        system_prompt = (
            "당신은 유튜브 영상 기획 전문가입니다. "
            "시장 데이터를 분석하여 제목부터 전체 원고까지 영상 제작의 모든 단계를 한 번에 완성합니다. "
            "도입부는 문제제기/공감/손해/이득/사례 중 이 제품과 주제에 가장 잘 맞는 요소 2-3개를 선택해 자연스럽게 조합하세요. "
            "5단계를 모두 순서대로 쓰는 것이 아니라, 상황에 맞는 요소를 골라 결합하는 것이 핵심입니다. "
            "전체 원고는 실제 카메라 앞에서 말할 수 있는 구어체로 작성하세요. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""키워드: "{keyword}"
내 채널/제품: {product_desc}

== 유튜브 상위 영상 데이터 ==
{videos_text}

== 네이버 카페 반응 ==
{naver_text}

위 시장 데이터를 바탕으로 영상 제작의 모든 단계를 포함한 완성된 기획안을 작성하세요.

[도입부 공식 선택 원칙]
- 정보/꿀팁 콘텐츠 → 공감 + 이득 조합 추천
- 신제품 소개 → 문제제기 + 이득 + 사례 조합 추천
- 비교/검증 → 공감 + 손해 + 이득 조합 추천
- 어떤 요소를 선택했는지 formula 필드에 명시하고, 이유도 설명할 것

{{
  "concept": "이 영상의 핵심 컨셉 한 줄 (제작 방향 잡는 문장)",
  "market_summary": "시장 상황과 시청자 핵심 욕구 2-3문장",
  "viewer_desires": {{
    "curiosity": ["구체적 궁금증 5-6개"],
    "complaints": ["구체적 불만/페인포인트 5-6개"],
    "wants": ["시청자가 원하는 것 5-6개"]
  }},
  "titles": [
    {{
      "title": "제목 (30자 내외)",
      "strategy": "클릭 심리 전략 (공포/호기심/이득/비교 등)",
      "hook_reason": "클릭하고 싶어지는 이유"
    }}
  ],
  "thumbnails": [
    {{
      "main_text": "썸네일 메인 문구 (5-10자, 강렬하게)",
      "sub_text": "서브 문구 (없으면 빈 문자열)",
      "visual": "이미지/배경/구도 설명 (촬영자가 바로 재현 가능하게)",
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
    "script": "완성된 도입부 대본 (30-50초 분량, 구어체)",
    "hook_line": "첫 문장 (스크롤을 멈추게 하는)"
  }},
  "script_sections": [
    {{
      "name": "섹션명",
      "timestamp": "00:00-01:30",
      "content": "이 섹션에서 다룰 핵심 내용",
      "script": "실제 대본 (구어체, 100-200자)",
      "filming_tip": "이 장면 촬영 팁"
    }}
  ],
  "cta": "영상 마지막 댓글/구독 유도 대본 (구어체)",
  "estimated_duration": "예상 영상 길이 (예: 5-7분)",
  "must_include": ["반드시 넣어야 할 내용 6-8개"],
  "differentiation": ["차별화 포인트 4-5개 (근거 포함)"]
}}

titles는 5개, thumbnails는 3개, script_sections는 영상 흐름에 맞게 4-7개 작성하세요."""

        content: list = []
        if thumb_blocks:
            content.append({"type": "text", "text": "상위 영상 썸네일 참고:\n"})
            content.extend(thumb_blocks)
        content.append({"type": "text", "text": user_text})

        msg = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        return _safe_json(msg.content[0].text.strip())

    async def analyze_shortform(self, keyword: str, product_desc: str, duration: str, videos: List[Dict] = None, naver: List[Dict] = None) -> Dict:
        market_section = ""
        if videos:
            market_section += f"\n== 유튜브 시장 데이터 (시청자 욕구·관심사 분석용) ==\n{self._build_videos_text(videos)}\n"
        if naver:
            market_section += f"\n== 네이버 카페 반응 ==\n{self._build_naver_text(naver)}\n"

        system_prompt = (
            "당신은 인스타그램 릴스 전문 콘텐츠 전략가입니다. "
            "시장 데이터(유튜브 댓글, 네이버 카페)를 분석해 시청자가 진짜 원하는 것을 파악하고, "
            "인스타그램 알고리즘에서 저장·공유·댓글이 노출을 결정한다는 것을 알고, "
            "이 세 가지 지표를 극대화하는 숏폼 콘텐츠를 기획합니다. "
            "첫 1-3초 훅이 스크롤을 멈추게 해야 하며, 자막/텍스트 오버레이로 음소거 시청도 소화 가능해야 합니다. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""주제/키워드: "{keyword}"
내 제품/서비스/채널: {product_desc or "없음"}
영상 길이: {duration}초
{market_section or "시장 데이터 없음 — 키워드 기반으로 분석"}

인스타그램 릴스 알고리즘 핵심: 저장 > 공유 > 댓글 > 좋아요 순으로 노출에 영향.
위 시장 데이터와 정보를 바탕으로 아래 JSON 형식으로 숏폼 기획안을 작성하세요.

{{
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

hooks는 3개, script는 {duration}초에 맞게 장면을 나눠 작성하세요."""

        msg = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        return _safe_json(msg.content[0].text.strip())

    async def analyze_edit_feedback(self, keyword: str, script: str, videos: List[Dict], naver: List[Dict]) -> Dict:
        videos_text = self._build_videos_text(videos)
        naver_text = self._build_naver_text(naver)

        system_prompt = (
            "당신은 유튜브 영상 편집 전략 전문가입니다. "
            "시장 데이터(경쟁 영상 댓글, 시청자 반응)를 기반으로 제공된 영상 대본을 분석하여 "
            "편집 방향에 대한 구체적이고 실행 가능한 피드백을 제공합니다. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        user_text = f"""키워드: "{keyword}"

== 시장 데이터: 유튜브 상위 영상 ==
{videos_text}

== 시장 데이터: 네이버 카페 반응 ==
{naver_text}

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
  "hook_feedback": "인트로 첫 30초에 대한 구체적 피드백 (현재 후킹이 충분한지, 어떻게 개선할지)",
  "edit_flow_suggestions": ["편집 흐름/순서 개선점 4-6개 (구체적으로)"],
  "missing_content": ["시청자가 원하는데 대본에 없는 내용 4-6개"],
  "recommended_titles": [
    {{
      "title": "클릭률 최적화 제목",
      "hook_reason": "왜 클릭하고 싶어지는지",
      "target_emotion": "유발하는 감정"
    }}
  ],
  "thumbnail_recommendations": [
    {{
      "concept": "썸네일 전체 컨셉",
      "main_text": "썸네일에 들어갈 메인 텍스트 (짧고 강렬하게)",
      "visual_element": "이미지/배경 요소 설명",
      "reason": "왜 이 썸네일이 클릭을 유도하는지"
    }}
  ]
}}"""

        msg = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        return _safe_json(msg.content[0].text.strip())
