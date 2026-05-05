#!/bin/bash
set -e
cd "$(dirname "$0")"

# Install dependencies if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "패키지 설치 중..."
  pip3 install -r requirements.txt
fi

echo ""
echo "======================================="
echo "  유튜브 콘텐츠 리서처 시작"
echo "======================================="
echo "  브라우저에서 열기: http://localhost:8000"
echo "  종료: Ctrl+C"
echo "======================================="
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000
