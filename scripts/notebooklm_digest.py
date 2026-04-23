#!/usr/bin/env python3
"""
arXivTimes NotebookLM Digest

daily_digest.py が出力した記事一覧 (articles.json) を読み、
NotebookLM 上に1つの新規ノートブックを作成して全URLをsource追加し、
日本語のAudio Overview を生成する。

生成された音声はダウンロードせず、ユーザーの NotebookLM 内に残す。
認証は環境変数 NOTEBOOKLM_AUTH_JSON（storage_state.json の内容）を利用する。
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from notebooklm import NotebookLMClient

ARTICLES_INPUT_PATH = os.environ.get("ARTICLES_INPUT_PATH", "/tmp/articles.json")
NOTEBOOK_TITLE_PREFIX = "AI/DX News Digest"
AUDIO_LANGUAGE = "ja"
AUDIO_INSTRUCTIONS = (
    "以下の記事群について、日本語で分かりやすく解説する Audio Overview を作成してください。"
    "AI・マーケティング・データ・DX の最新動向を、初心者にも理解できるように噛み砕いて説明し、"
    "各トピックのビジネスインパクトにも触れてください。"
)


def load_articles(path: str) -> list[dict]:
    if not os.path.exists(path):
        print(f"Error: articles file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        articles = json.load(f)
    if not articles:
        print("Error: articles list is empty", file=sys.stderr)
        sys.exit(1)
    return articles


def build_notebook_title() -> str:
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")
    return f"{NOTEBOOK_TITLE_PREFIX} {today}"


async def run() -> None:
    if not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        print("Error: NOTEBOOKLM_AUTH_JSON is not set", file=sys.stderr)
        sys.exit(1)

    articles = load_articles(ARTICLES_INPUT_PATH)
    urls = [a.get("url") for a in articles if a.get("url")]
    if not urls:
        print("Error: no valid URLs found in articles", file=sys.stderr)
        sys.exit(1)

    title = build_notebook_title()
    print(f"Creating NotebookLM notebook: {title}")
    print(f"Adding {len(urls)} sources...")

    async with await NotebookLMClient.from_storage() as client:
        nb = await client.notebooks.create(title)
        print(f"Notebook created: id={nb.id}")

        added = 0
        for i, url in enumerate(urls, 1):
            try:
                await client.sources.add_url(nb.id, url, wait=True)
                added += 1
                print(f"  [{i}/{len(urls)}] added: {url}")
            except Exception as e:
                print(f"  [{i}/{len(urls)}] failed: {url} ({e})", file=sys.stderr)

        if added == 0:
            print("Error: no sources could be added", file=sys.stderr)
            sys.exit(1)

        print(f"Generating Japanese Audio Overview (sources={added})...")
        status = await client.artifacts.generate_audio(
            nb.id,
            instructions=AUDIO_INSTRUCTIONS,
            language=AUDIO_LANGUAGE,
        )
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        print("Audio Overview generation completed.")
        print(f"Open: https://notebooklm.google.com/notebook/{nb.id}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
