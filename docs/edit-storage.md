# AI 편집 디렉터 저장 정책

## 현재 production 구조

- SQLite audit data: `/data/history.db`
- 편집 원본과 결과: `/data/edit_projects/<project_uuid>`
- 업로드 multipart spool과 프로세스 임시 파일: Render ephemeral `/tmp`
- `/data`는 persistent disk이므로 재배포와 프로세스 재시작 후에도 유지된다.
- `/tmp`는 영속성을 기대하지 않으며 어떤 프로젝트 source of truth로도 사용하지 않는다.

## 수명 주기

- `uploaded`부터 `rendering`까지의 active 프로젝트는 자동 cleanup에서 항상 제외한다.
- 완료 원본은 기본 72시간, 최종 full/short/EDL은 기본 30일 보존한다.
- ffmpeg는 숨김 `.part.mp4`에 쓰고 성공한 뒤 원자적으로 최종 파일과 교체한다.
- 긴 영상은 타임라인 구간별 seek-bounded input과 제한된 ffmpeg thread를 사용해
  뒤쪽 프레임이 메모리에 계속 쌓이지 않게 한다.
- 분석 오디오는 transcription 종료 시, render part는 성공·실패 시 즉시 삭제한다.
- 중단된 orphan temp는 1시간, DB가 없는 orphan project directory는 24시간 뒤 정리한다.
- 수동 정리는 파일만 지우고 SQLite의 편집 결정, 대화, 성과 학습 audit row는 보존한다.

모든 기간과 여유 공간은 `.env.example`의 `EDIT_*` 설정으로 조정할 수 있다.

## 대용량 확장

`edit_storage.ObjectStorageBackend`는 Cloudflare R2와 S3 호환 API를 위한 계약이다.
현재 production은 credential이 없어 `LocalStorageBackend`를 사용한다. 10~60분 원본을
상시 보관하려면 R2/S3를 연결하고 local persistent disk는 ffmpeg working set과 cache로만
사용하는 구성이 적합하다.

필요 환경변수:

- `EDIT_STORAGE_BACKEND=object`
- `EDIT_OBJECT_ENDPOINT`
- `EDIT_OBJECT_BUCKET`
- `EDIT_OBJECT_REGION` (`auto` for R2)
- `EDIT_OBJECT_PREFIX`
- `EDIT_OBJECT_ACCESS_KEY_ID`
- `EDIT_OBJECT_SECRET_ACCESS_KEY`

credential이 없는 상태에서는 object backend를 초기화하거나 외부 요청을 보내지 않는다.
