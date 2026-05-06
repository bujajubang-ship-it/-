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
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        raise ValueError("AI 응답이 도중에 잘렸습니다. 다시 시도해주세요.")


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
            parts.append(
                f"[영상{i}] {v['title']}\n"
                f"조회수:{v['view_count']:,} / 좋아요:{v['like_count']:,} / 댓글:{v['comment_count']:,}\n"
                f"채널:{v['channel']} / 업로드:{v['published_at']}\n"
                f"URL:{v['url']}\n"
                f"설명:{v['description'][:300]}\n"
                f"인기댓글(좋아요순):\n{comments_block or '  (댓글 없음)'}\n"
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
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        product_info = f"\n이번 영상 제품 상세 정보:\n{product_desc}" if product_desc.strip() else ""
        user_text = f"""영상 키워드: "{keyword}"{product_info}

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
  "content_type": "풀링+키 또는 풀링 또는 키",
  "content_type_reason": "왜 이 유형인지, 풀링+키라면 어떻게 자연스럽게 판매로 연결되는지",
  "sell_angle": "이 영상으로 자연스럽게 노출할 제품·서비스 (풀링 단독이면 빈 문자열)",
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
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
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
            "채널: 부자주방 — 외식업 운영자(식당·분식집·한식당 사장님) 대상 업소용 주방용품 전문 채널. "
            "시장 데이터(유튜브 댓글, 네이버 카페)를 분석해 시청자가 진짜 원하는 것을 파악하고, "
            "인스타그램 알고리즘에서 저장·공유·댓글이 노출을 결정한다는 것을 알고, "
            "이 세 가지 지표를 극대화하는 숏폼 콘텐츠를 기획합니다. "
            "첫 1-3초 훅이 스크롤을 멈추게 해야 하며, 자막/텍스트 오버레이로 음소거 시청도 소화 가능해야 합니다. "
            "반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만."
        )

        product_info = f"\n이번 릴스 제품 상세: {product_desc}" if product_desc.strip() else ""
        user_text = f"""주제/키워드: "{keyword}"{product_info}
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
        videos_text = self._build_videos_text(videos, max_videos=10, max_comments=30)
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
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        return _safe_json(msg.content[0].text.strip())

    async def analyze_channel(self, channel_info: Dict, videos: List[Dict]) -> Dict:
        from collections import defaultdict

        top10 = sorted(videos, key=lambda x: x["view_count"], reverse=True)[:10]
        recent50 = videos[:50]

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
            f"{v['published_at']} ({v['publish_day']}요일 {v['publish_hour']}시) | {v['title']} | 조회수:{v['view_count']:,} | {v['duration_sec']//60}분"
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
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system="당신은 유튜브 채널 성장 전략 전문가입니다. 데이터 기반으로 구체적인 인사이트를 제공합니다. 반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만.",
            messages=[{"role": "user", "content": prompt}],
        )
        return _safe_json(msg.content[0].text.strip())

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
            model="claude-sonnet-4-6",
            max_tokens=3000,
            system="당신은 유튜브 크리에이터 전략 컨설턴트입니다. 데이터와 트렌드 기반으로 구체적인 업로드 전략을 제시합니다. 반드시 유효한 JSON만 출력하세요. 마크다운 코드블록 없이 순수 JSON만.",
            messages=[{"role": "user", "content": prompt}],
        )
        return _safe_json(msg.content[0].text.strip())
