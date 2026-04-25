#!/usr/bin/env python3
"""
arXivTimes Daily Digest
AI・マーケティング・データ・DX関連の最新ニュースを収集し、Slackに投稿する。
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta

import anthropic
import requests

SLACK_CHANNEL_ID = "C0AU65Z4TAL"  # #03y_arxivtimes
ARTICLES_OUTPUT_PATH = os.environ.get("ARTICLES_OUTPUT_PATH", "/tmp/articles.json")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
REQUEST_TIMEOUT = 120.0
MAX_RETRIES = 3

SEARCH_QUERIES = [
    "AI artificial intelligence latest news today",
    "マーケティング AI DX データ活用 最新ニュース",
    "AI machine learning research breakthroughs today",
    "generative AI enterprise data analytics news",
]

SYSTEM_PROMPT = """あなたはAI・マーケティング・データ・DX分野の最新ニュースキュレーターです。
Web検索結果から、直近24時間の重要記事を選定し、日本語で要約してください。

以下のルールに従ってください：
- 最大10件の記事を選定
- AI、マーケティング、データ分析、DXの4分野をバランスよくカバー
- 重複する内容の記事は除外
- 日本語の記事も英語の記事も対象（タイトル・要約は日本語で記載）
- 出力はJSON配列で返す（各要素: source, title, url, summary（3要素の配列）, detail）
- source: 記事の発信元メディア・サイト名（例: TechCrunch, ITmedia, 日経XTECH, MIT Technology Review）
- detail: 記事内容を日本語1文〜2文で200字以内にまとめた説明（必ず200字以下）
"""

USER_PROMPT_TEMPLATE = """以下のWeb検索結果から、AI・マーケティング・データ・DX関連の最新ニュース（直近24時間）を最大10件選定してください。
今日の日付: {today}

検索結果を分析し、以下のJSON形式で出力してください（JSONのみ、説明不要）:

```json
[
  {{
    "source": "メディア・サイト名",
    "title": "記事タイトル（日本語）",
    "url": "https://...",
    "summary": ["要約1行目", "要約2行目", "要約3行目"],
    "detail": "記事内容を200字以内でまとめた日本語説明"
  }}
]
```
"""


def collect_news() -> list[dict]:
    """Claude APIのWeb検索ツールでニュースを収集し、構造化して返す。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("Error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
    )

    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y年%m月%d日")

    # Web検索を使ってニュースを収集
    search_results_text = ""
    for query in SEARCH_QUERIES:
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                messages=[
                    {
                        "role": "user",
                        "content": f"Search the web for: {query}\n\nList the top 5 results with title, URL, and a brief description.",
                    }
                ],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 3,
                    }
                ],
            )
            for block in response.content:
                if block.type == "text":
                    search_results_text += f"\n\n--- Search: {query} ---\n{block.text}"
        except Exception as e:
            print(
                f"Warning: Search failed for '{query}': {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
            if cause is not None:
                print(
                    f"  Underlying cause: {type(cause).__name__}: {cause}",
                    file=sys.stderr,
                )

    if not search_results_text:
        print("Error: No search results collected", file=sys.stderr)
        sys.exit(1)

    # 検索結果をClaude APIで構造化
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(today=today)
                + "\n\n"
                + search_results_text,
            }
        ],
    )

    result_text = ""
    for block in response.content:
        if block.type == "text":
            result_text += block.text

    # JSONを抽出
    json_start = result_text.find("[")
    json_end = result_text.rfind("]") + 1
    if json_start == -1 or json_end == 0:
        print(f"Error: Could not parse JSON from response:\n{result_text}", file=sys.stderr)
        sys.exit(1)

    articles = json.loads(result_text[json_start:json_end])
    return articles[:10]


def format_slack_message(articles: list[dict]) -> str:
    """記事リストをSlackメッセージ形式に整形する。"""
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y/%m/%d")

    lines = [f":newspaper: *AI・マーケティング・データ・DX 最新ニュースまとめ*（{today}）\n"]

    for i, article in enumerate(articles, 1):
        title = article.get("title", "No Title")
        url = article.get("url", "")
        summary = article.get("summary", [])

        lines.append(f"*{i}. {title}*")
        if url:
            lines.append(url)
        for s in summary:
            lines.append(f"• {s}")
        lines.append("")

    lines.append("_arXivTimes Daily Digest by Claude_")
    return "\n".join(lines)


def post_to_slack(message: str) -> None:
    """Slack chat.postMessage APIでメッセージを投稿する。"""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print("Error: SLACK_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "text": message,
            "unfurl_links": True,
        },
        timeout=30,
    )

    data = resp.json()
    if not data.get("ok"):
        print(f"Error: Slack API error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)

    print(f"Message posted to #{SLACK_CHANNEL_ID} successfully.")


def save_articles(articles: list[dict], path: str) -> None:
    """後続ステップ（スプレッドシート追記等）のために記事一覧をJSONで出力する。

    各記事に collected_at（JST、ISO8601）を付与する。
    """
    jst = timezone(timedelta(hours=9))
    collected_at = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
    enriched = [{**a, "collected_at": collected_at} for a in articles]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(enriched)} articles to {path}")
    except Exception as e:
        print(f"Warning: failed to save articles to {path}: {e}", file=sys.stderr)


def main():
    print("Collecting latest news...")
    articles = collect_news()
    print(f"Found {len(articles)} articles.")

    message = format_slack_message(articles)
    print("---\n" + message + "\n---")

    post_to_slack(message)
    save_articles(articles, ARTICLES_OUTPUT_PATH)
    print("Done!")


if __name__ == "__main__":
    main()
