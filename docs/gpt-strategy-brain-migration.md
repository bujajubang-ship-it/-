# GPT Strategy Brain 아키텍처와 migration 계획

## 목표 구조

```text
FastAPI endpoint
  -> StrategyBrain.run(mode, task input, content_id)
      -> common strategy policy
      -> mode registry
      -> read-only tool registry
      -> OpenAI Responses provider
      -> mode output schema
  -> 기존 endpoint JSON/SSE adapter
```

공통 policy는 태도와 판단 절차만 가진다. 채널 데이터·지식·과거 대화는 prompt 상수가 아니라 tool로 조회한다.

## 공통 판단 원칙

- 사용자의 의견에 자동 동의하지 않는다.
- 데이터 → 패턴 → 원인 가설 → 의미 → 선택지 → 반대 근거 → 추천 순서로 판단한다.
- 조회할 수 있는 사실은 추측하지 않고 tool을 사용한다.
- 없는 숫자를 0으로 만들지 않고 `null/unavailable`로 둔다.
- AI 제안, 사용자 결정, 측정된 사실을 구분한다.
- 판단에 사용한 근거의 출처·수집일·측정기간·표본수를 보존한다.
- read-only 분석 요청이 외부 쓰기나 DB 변경으로 확대되지 않게 한다.

## tool 계층

모델에 raw SQL이나 범용 HTTP 권한을 주지 않는다. application repository를 감싸는 고수준 read-only tool만 제공한다.

- 채널/성과: `get_recent_videos`, `get_channel_performance`, `compare_recent_videos`, `compare_similar_videos`, `get_video_metrics`
- 콘텐츠: `get_content_project`, `search_past_plans`, `get_worksheet`, `get_video_feedback`
- 지식/기억: `search_businesspt_knowledge`, `search_knowledge`, `search_strategy_decisions`, `search_previous_conversations`
- 운영: `get_pipeline_items`
- 외부: `search_youtube`, `get_youtube_comments`, `search_naver_cafe`, `search_market_trends`

각 결과는 `source`, `collected_at`, `period`, `freshness`, `sample_size`, `data`, `unavailable_reason`를 동일하게 반환한다.

## content_id와 학습 순환

```text
contents
  -> plans / worksheets / assets(title, thumbnail, hook)
  -> feedback / edit_actions
  -> publication / metric snapshots / retention points
  -> postmortems / content_lessons
  -> 다음 콘텐츠 retrieval
```

피드백 문장 자체와 실제 적용 여부를 분리한다. `edit_actions`에 제안·채택·실행 상태와 구간을 저장하고, 업로드 후 metric window와 연결해 검증해야 한다.

## 장기 메모리 승격 규칙

- `ai_proposal`: AI가 제안했지만 사용자가 결정하지 않음
- `user_decision`: 사용자가 명시적으로 채택/거절
- `hypothesis`: 검증할 전략 가설
- `observed_result`: Analytics로 측정된 사실
- `content_lesson`: 여러 관찰을 근거로 승인된 교훈

AI 추측은 자동으로 `confirmed_rule`이 되지 않는다. 사용자 승인 또는 정해진 표본·검증 기준을 통과해야 한다.

## 단계별 migration

1. Claude inventory, 공통 계약, mode/tool registry와 eval baseline 작성
2. PostgreSQL 영속성, Analytics 정확성, 인증/CORS/secret 정리
3. OpenAI Responses provider를 feature flag 뒤에 추가하고 어떤 endpoint에도 기본 적용하지 않음
4. 전략 상담 shadow test 후 선택적 GPT 전환
5. 미드폼·숏폼·제목·썸네일·워크시트 전환
6. 영상/편집 피드백 전환과 프레임·content context 연결
7. 채널 분석·주제추천·나머지 구조화 기능 전환
8. 내부 retrieval tool과 BusinessPT·과거 콘텐츠 검색 강화
9. 구조화 장기 메모리와 사용자 결정 확인 흐름 도입
10. 기획 가설·편집 행동·실제 성과를 잇는 feedback loop 도입
11. 새 Analytics snapshot에서 이상을 찾는 `proactive_insights` 도입
12. 회귀·품질 eval 통과 후 Anthropic 제거

## rollout과 rollback

- `STRATEGY_BRAIN_PROVIDER=anthropic|openai`를 전체 kill switch로 둔다.
- 기능별 `GPT_MODE_*_ENABLED` flag로 한 mode씩 전환한다.
- shadow mode에서는 GPT 결과를 사용자에게 노출하지 않고 Claude 결과와 평가한다.
- 기존 endpoint 응답 schema를 adapter가 유지한다.
- Claude SDK와 기존 `Analyzer`는 전체 회귀 검증 전까지 삭제하지 않는다.
- DB 파괴적 migration, Render 환경변수 변경, production 배포는 별도 승인 단계로 둔다.

## 품질 gate

- output schema 유효성 및 필수 필드 충족
- 실제로 호출해야 할 tool을 호출했는지
- 근거에 source/date/period/sample size가 있는지
- 없는 데이터를 만들어내지 않았는지
- 제목·썸네일·훅·본문·워크시트가 같은 전략 가설을 따르는지
- 영상 피드백이 실제 타임스탬프와 원래 기획을 인용하는지
- 사용자 의견에 반대할 때 데이터 근거가 있는지
- 기존 프런트 JSON/SSE 계약이 유지되는지
