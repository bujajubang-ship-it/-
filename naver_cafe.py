import requests
import pandas as pd
import os

CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")

headers = {
    "X-Naver-Client-Id": CLIENT_ID,
    "X-Naver-Client-Secret": CLIENT_SECRET
}

keywords = ["업소용주방기기 창업", "주방집기 어디서", "냉장고 창업 추천", "주방기기 사기", "식당창업 주방"]

all_results = []
for kw in keywords:
    url = f"https://openapi.naver.com/v1/search/cafearticle.json?query={kw}&display=100&sort=sim"
    res = requests.get(url, headers=headers)
    items = res.json().get("items", [])
    for item in items:
        all_results.append({
            "keyword": kw,
            "title": item["title"].replace("<b>","").replace("</b>",""),
            "description": item["description"].replace("<b>","").replace("</b>",""),
            "link": item["link"]
        })
    print(f"{kw}: {len(items)}건 수집")

df = pd.DataFrame(all_results)
df.to_csv("naver_cafe.csv", index=False, encoding="utf-8-sig")
print(f"\n완료! 총 {len(df)}건 → naver_cafe.csv 저장")
