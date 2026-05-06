"""
YouTube OAuth 인증 스크립트
실행: python3 auth.py
"""
import json
import os
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>인증 완료! 이 창을 닫아도 됩니다.</h2>".encode())

    def log_message(self, *args):
        pass


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: .env 파일에 OAUTH_CLIENT_ID와 OAUTH_CLIENT_SECRET을 먼저 입력하세요.")
        return

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        })
    )

    print("\n브라우저에서 tjdrjs1248@gmail.com 계정으로 로그인하세요.")
    print("(자동으로 브라우저가 열립니다)\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()

    if not auth_code:
        print("인증 실패: 코드를 받지 못했습니다.")
        return

    # 인증 코드 → 토큰 교환
    data = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())

    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        print("ERROR: refresh_token을 받지 못했습니다. Google Cloud Console에서 테스트 사용자로 추가됐는지 확인하세요.")
        return

    print("\n✅ 인증 성공!")
    print(f"\n.env 파일에 아래 줄을 추가하세요:\n")
    print(f"OAUTH_REFRESH_TOKEN={refresh_token}\n")

    # .env 자동 추가
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "a") as f:
        f.write(f"\nOAUTH_REFRESH_TOKEN={refresh_token}\n")
    print(".env 파일에 자동으로 저장됐습니다.")


if __name__ == "__main__":
    main()
