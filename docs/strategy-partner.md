# 부자주방 콘텐츠 전략 파트너

## 실행 구조

사용자 질문은 OpenAI Responses API의 `gpt-5.6-sol`로 전달된다. 모델은 질문에 필요한 범위만 다음 read-only retrieval tool로 조회한다.

- 채널 성과: `get_recent_channel_performance`, `get_video_performance`, `compare_similar_videos`, `get_retention_patterns`
- 지식: `search_knowledge`, `search_business_pt_knowledge`
- 기존 작업: `search_previous_plans`, `search_previous_worksheets`, `get_content_pipeline`
- 학습 이력: `search_feedback_history`, `search_chat_memory`, `get_recent_trends`

각 tool 결과에는 source, 수집일, 측정 기간, freshness, sample size와 데이터 부재 사유가 포함된다. 모델은 raw SQL, 임의 HTTP 요청, 쓰기 tool을 받지 않는다. 전략 요청은 기본 `max`, 짧은 조회는 mode에 따라 `high` reasoning을 사용한다. OpenAI가 첫 토큰 전에 실패하면 기존 Claude 상담으로만 fallback한다.

## 공통 전략과 feedback loop

`content_strategies`는 미드폼·숏폼·상담이 공유하는 하나의 전략 context를 저장한다. 주제, 타깃, why-now, 핵심 메시지, 근거, 제목, 썸네일, 훅, 구조, 촬영 컷, 워크시트와 KPI를 한 객체로 유지한다.

업로드 후 `/api/strategies/{id}/link-video`로 YouTube video ID를 연결한다. 자동 수집이 새 snapshot을 저장하면 `performance_checkpoints`가 D1/D3/D7/D14/D30/long 성과와 최신 retention snapshot을 원래 기획에 연결한다. 이 기록은 이후 `search_feedback_history`의 근거가 된다.

## 자동 수집

Render web service에 연결된 영속 SQLite disk는 별도 Cron service와 공유되지 않는다. 따라서 web process 안의 단일 lease 기반 scheduler가 기본 6시간 간격으로 수집한다.

- Data API metadata를 먼저 upsert한다.
- Analytics aggregate snapshot과 최근 45일 daily metric을 저장한다.
- 한 실행당 오래된 retention 대상 20개를 순환 수집한다.
- Reporting API Reach 파일을 report ID와 SHA-256 기준으로 한 번만 가져온다.
- 동일 fingerprint의 snapshot은 중복 저장하지 않는다.
- source별 오류는 기존 정상 row를 삭제하지 않고 collection 상태의 warning으로 남긴다.
- `/api/analytics/refresh`는 owner가 수동 실행할 수 있고 `/api/analytics/status`에서 `data_through`와 지연 상태를 확인한다.

`youtube_collection_state`의 lease는 process 중첩 실행을 막는다. 여러 web instance를 동시에 운영하려면 SQLite 단일 writer 전제를 재검토하고 별도 durable job queue 또는 PostgreSQL로 옮겨야 한다.

## Production 설정

secret은 Render 환경변수로만 설정한다.

- `OPENAI_API_KEY`
- 기존 `YOUTUBE_API_KEY`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REFRESH_TOKEN`
- owner auth secret들

비secret 기본값은 `render.yaml`에 있다. OpenAI response 저장은 기본 비활성이다. Rollback은 `STRATEGY_BRAIN_PROVIDER=anthropic`로 가능하며 기존 Claude 코드와 SSE UI 계약은 유지된다.

YouTube Reporting API가 Google Cloud 프로젝트에서 꺼져 있으면 Reach/CTR만 `SERVICE_DISABLED` warning으로 건너뛴다. Analytics, retention과 기존 snapshot은 계속 보존된다. API를 활성화한 뒤 첫 Reach report가 생성되기까지 Google 측 지연이 있을 수 있다.

## 배포 전 데이터 안전 gate

1. Render shell에서 `/data/backups`처럼 명시적인 backup directory를 만들고 다음 명령으로 SQLite online backup 생성: `python production_preflight.py --backup /data/backups/history-before-strategy.db`
2. backup SHA-256과 `PRAGMA integrity_check` 확인
3. 기존 history/pipeline/worksheet/chat/knowledge/optimize row count 기록
4. additive migration 후 기존 table count와 schema 재확인
5. `/healthz`, `/login`, owner auth, collection status 확인

이 gate 없이 위험했던 `55cc94c` 기준 main을 production에 바로 배포하지 않는다.
