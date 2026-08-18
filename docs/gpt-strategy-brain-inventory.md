# GPT Strategy Brain 전환 inventory

## 현재 호출 구조

- `main.py`가 검색·전사·영상 처리 등 비-AI 데이터를 준비한다.
- `Analyzer`가 모든 활성 지식을 기본으로 불러온다.
- 구조화 작업 17개는 `Analyzer._create()`를 거쳐 Claude Opus 5를 호출한다.
- 전략 상담은 `Analyzer.chat_stream()` 안에서 Claude streaming/tool loop를 별도로 구현한다.
- 구조화 응답은 실제 schema 강제가 아니라 프롬프트의 JSON 예시와 `_safe_json()` 복구 파싱에 의존한다.
- 상담 tool은 YouTube·댓글·네이버 검색 등 외부 데이터 6개뿐이다. DB, Analytics, 과거 기획, 파이프라인을 조회하는 tool은 없다.

## Claude 사용 기능 18개

| mode | endpoint / 함수 | 현재 주요 입력 | 현재 출력·저장 | 전환 시 보존할 계약 |
|---|---|---|---|---|
| research | `/api/analyze` → `analyze` | 키워드, YouTube 영상·댓글·썸네일·Most Replayed, 네이버 카페 | 시장·욕구·경쟁·제목·썸네일·구성 JSON, `history:research` | 기존 JSON 키와 SSE 단계 |
| planning | `/api/planning` → `analyze_planning` | 키워드, 제품 설명, 시장 인사이트 | 문제정의·제목·썸네일 JSON, `history:planning` | 프런트 출력 키 |
| intro | `/api/intro` → `write_intro` | 키워드, 제품 설명, 문제정의, 시청자 욕구 | 도입부 후보·전략 JSON, `history:intro` | 도입부 JSON 계약 |
| script | `/api/script` → `write_script` | 키워드, 제품 설명, 레퍼런스 원고, 문맥 | 전체 원고 JSON, `history:script` | 긴 원고와 JSON 계약 |
| midform_planning | `/api/midform` → `analyze_midform` | 검색 영상·댓글·썸네일, 네이버, ViewTrap, 제품, 지식 | 미드폼 종합 기획 JSON, `history:midform` | 제목→썸네일→도입→본문의 연결 |
| shortform_planning | `/api/shortform` → `analyze_shortform` | 키워드, 제품, 길이, YouTube·네이버, 지식 | 릴스 훅·장면·캡션 JSON, `history:shortform` | 시간대별 script 배열 |
| topic_discovery | `/api/topic-suggest` → `analyze_topic_trends` | 경쟁/급상승 YouTube, 네이버 | 추천 주제·근거 JSON, `history:topic` | 풀링/키 분류 |
| youtube_search_analysis | `/api/yt-search` → `analyze_search` | 검색/급상승 결과 | 패턴·기회·추천 JSON, `history:yt-search` | 원본 영상 목록과 AI report 병행 |
| channel_analysis | `/api/channel-analyze` → `analyze_channel` | 채널·영상 공개정보, 일부 Analytics | 채널 패턴·병목·다음 전략 JSON, `history:channel` | Analytics 실패 fallback |
| upload_decision | `/api/video-decision` → `analyze_video_decision` | 후보 영상 수동 입력, 현재 날짜 | 업로드 우선순위 JSON, `history:decision` | 후보별 판단 구조 |
| edit_feedback | `/api/edit-feedback` → `analyze_edit_feedback` | 대본, 검색 영상·댓글, 네이버, ViewTrap | 유지·삭제·보완·제목·썸네일 JSON, `history:edit` | 구체 대본 인용과 기존 키 |
| video_feedback | `/api/video-feedback` → `analyze_video_feedback` | 타임스탬프 전사, 활성 지식 | 편집 피드백 JSON, 전사와 함께 `history:video_feedback` | ffmpeg·전사·SSE·타임스탬프 |
| worksheet | `/api/worksheet/autofill` → `autofill_worksheet` | 키워드·brief, 레퍼런스 대본·댓글·썸네일, 네이버, ViewTrap, 지식 | `WS_COLS`와 1:1 JSON, `worksheet_rows` | 워크시트 필드명 |
| jjachi | `/api/jjachi` → `plan_jjachi` | 사용자 답변, YouTube·네이버, 지식 | 제목·썸네일·도입·본문 JSON, `history:jjachi` | 사용자 사실을 지어내지 않는 규칙 |
| sns_conversion | `/api/sns-convert` → `analyze_sns_convert` | 키워드, 원본 대본 | 블로그·스레드·숏폼 변환 JSON, `history:sns` | 채널별 결과 키 |
| detail_page | `/api/detail-page` → `analyze_detail_page` | 제품·가격·타깃, YouTube·네이버 | 상세페이지 카피·구성 JSON, `history:detail_page` | 기존 상세페이지 출력 |
| blog | `/api/blog` → `analyze_blog` | 키워드, 메모, 사진, 지역, 링크 | 사진 배치 포함 블로그 JSON, `history:blog` | 이미지 입력·결과 키 |
| strategy_chat | `/api/chat` → `chat_stream` | 메시지, 최근 20개 대화, 첨부, 활성 지식 전체 | markdown token stream, 별도 `chat_session` 저장 | token SSE와 첨부 지원 |

## 현재 중복과 구조적 한계

1. 채널 설명, CTR 10%·초반 이탈 40% 목표, 제목·썸네일·도입 원칙이 여러 prompt에 반복된다.
2. 각 함수가 데이터 직렬화, prompt, 출력 schema를 한 파일 안에서 동시에 담당한다.
3. 활성 지식은 관련도 검색 없이 앞부분을 글자 예산만큼 잘라 대부분의 호출에 주입된다.
4. 상담만 tool loop를 가지며 나머지 mode는 필요한 내부 데이터를 스스로 조회할 수 없다.
5. `my_channel` tool은 YouTube Data API 공개정보만 사용하며 CTR·retention 등 소유자 Analytics가 아니다.
6. 영상 피드백은 현재 전사와 지식만 사용한다. 대표 프레임·원래 기획·워크시트·제목·썸네일·성과는 연결되지 않는다.
7. 응답 근거의 출처·수집일·기간·표본수가 표준화되어 있지 않다.
8. AI 제안, 사용자 결정, 검증된 성과 교훈을 구분하는 장기 메모리 구조가 없다.

## 반드시 재사용할 비-AI 기능

- `YouTubeService`, `NaverService`, ViewTrap/heatmap 수집
- YouTube OAuth 및 Analytics 수집의 정상 지표 부분
- ffmpeg/ffprobe 영상·오디오 처리와 타임스탬프 전사
- 썸네일 이미지 생성
- 기존 FastAPI endpoint와 SSE 이벤트 이름
- history, pipeline, worksheet, chat, knowledge CRUD의 사용자 경험

## 공통화 경계

`GPT Strategy Brain = 공통 전략 원칙 + mode별 task prompt + 허용 tool 집합 + output schema + 평가 기준`

화면은 데이터를 모두 조립해 prompt로 넘기지 않는다. mode가 허용한 read-only tool 중 실제 등록된 것만 모델에 노출하고, tool 결과는 공통 evidence envelope로 반환한다.
