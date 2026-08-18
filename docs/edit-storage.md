# Production 영상 편집 저장·작업 구조

## 안전 기본값

- SQLite 의사결정 원장: production `/data/history.db`
- credential 미설정 시 미디어: `/data/edit_projects/<project_uuid>`
- credential 설정 시 원본/full/short/EDL: R2 또는 S3-compatible bucket
- `/tmp`와 local project directory는 분석 audio chunk 등 재생성 가능한 작업 파일에만 쓴다.
- 기존 DB table을 변경하지 않는다. `edit_project`와 `edit_job`은 기존 `history`에 additive row로 저장한다.
- 원본·full·short는 기간만으로 자동 삭제하지 않는다. owner가 `업로드 완료 / 미디어 정리`를 명시적으로 실행해야 한다.
- 미디어 purge 뒤에도 approved plan, plan version diff, 대화, EDL decision, edit log, strategy/worksheet link, YouTube 성과와 memory는 남는다.

## Object Storage

다음 환경변수를 모두 설정하면 브라우저는 Render를 거치지 않고 multipart upload를 수행한다.

- `OBJECT_STORAGE_PROVIDER=r2` (`s3`도 지원)
- `OBJECT_STORAGE_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com`
- `OBJECT_STORAGE_BUCKET=<bucket>`
- `OBJECT_STORAGE_REGION=auto`
- `OBJECT_STORAGE_PREFIX=edit-projects`
- `OBJECT_STORAGE_ACCESS_KEY_ID=<secret>`
- `OBJECT_STORAGE_SECRET_ACCESS_KEY=<secret>`

secret은 DB, API 응답, UI, log에 기록하지 않는다. 설정이 빠지면 object API는 unavailable로 표시하고 기존 local backend가 유지된다.

R2 bucket CORS는 production origin과 local 개발 origin의 `PUT`, `GET`, `HEAD`를 허용하고 `ETag`를 expose해야 multipart resume가 작동한다. 예:

```json
[
  {
    "AllowedOrigins": ["https://<production-host>", "http://127.0.0.1:8765"],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

## Upload/recovery

- 기본 part 64 MiB, 최대 10,000 part, API 제한 100 GiB다.
- client upload fingerprint와 server-side multipart state로 같은 파일의 중복 start가 idempotent하다.
- 이미 완료된 part는 `list_parts`로 확인하고 건너뛴다.
- 각 part는 exponential delay로 네 번 재시도한다.
- 브라우저가 닫혀도 object와 multipart state는 유지된다. 같은 파일을 다시 선택하면 완료 part 이후부터 계속한다.
- R2 CORS에서 `ETag`가 노출되지 않으면 완료를 시도하지 않고 명시적 오류를 표시한다.

## Lifecycle과 durable queue

프로젝트 lifecycle:

`UPLOADING → UPLOADED → ANALYZING → AWAITING_REVIEW → APPROVED → QUEUED → RENDERING → COMPLETED → PUBLISHED_OR_DOWNLOADED → MEDIA_PURGED`

실패는 `FAILED_UPLOAD`, `FAILED_ANALYSIS`, `FAILED_RENDER`로 구분한다. 모든 전환은 `state_history`에 남는다.

job에는 project/type/status/attempt/max_attempts/queue/start/heartbeat/finish/error/next retry/worker/idempotency key/timing을 저장한다. production worker는 heavy media job을 한 번에 하나만 claim한다. 서버 재시작 뒤 180초 이상 stale heartbeat인 running job은 queued로 회수되며, 완료된 metadata/transcript/evidence/output은 재사용한다.

## Long-form 분석과 렌더

- audio는 기본 20분 단위로 추출·transcribe하고 원본 절대 타임코드로 다시 합친다.
- silence와 scene detection은 독립 실행한다.
- AI retrieval은 유사 영상, retention/relative retention, channel snapshot, Reporting Reach CTR, 비즈니스PT, 피드백, worksheet, content strategy, long-term/edit memory를 병렬 조회한다.
- ffmpeg는 승인된 seek-bounded timeline만 H.264/AAC로 렌더한다. object mode에서는 output을 local disk에 한 번 더 쓰지 않고 multipart stream으로 bucket에 보낸다.
- full/short는 ffprobe duration/video/audio 검증 후에만 `COMPLETED`가 된다. 초반 black frame/audio gap과 너무 짧은 컷은 QA warning으로 남는다.
- B-roll/자막은 실제 asset이 없을 때 자동 합성하지 않으며 정확한 타임코드 지시로 EDL에 남는다.

## Cleanup

- analysis audio chunk와 ffmpeg partial은 `finally`에서 즉시 지운다.
- 1시간 지난 temp와 DB가 없는 24시간 orphan directory만 자동 정리한다.
- queued/running/active project는 cleanup 대상에서 제외한다.
- user media는 owner 확인 없이 age-based cleanup하지 않는다.
- `media-purge`는 source/proxy/full/short만 지우며 decision artifact와 DB 원장은 보존한다.
