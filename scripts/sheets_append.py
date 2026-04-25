#!/usr/bin/env python3
"""
arXivTimes Sheets Append

daily_digest.py が出力した articles.json を読み、
指定されたGoogleスプレッドシート（gid指定タブ）に行を追記する。

列構成: ソース / タイトル / URL / 詳細(200字以内) / 収集日時

認証はサービスアカウントJSON（環境変数 GOOGLE_SERVICE_ACCOUNT_JSON）を使用する。
"""

import json
import os
import sys
import traceback
from urllib.parse import urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build

ARTICLES_INPUT_PATH = os.environ.get("ARTICLES_INPUT_PATH", "/tmp/articles.json")
SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1ih5hFlS8kwGgcSP_cau8b0VmYcWtfVjP3pml5RrpRe4"
)
TARGET_SHEET_GID = int(os.environ.get("TARGET_SHEET_GID", "1803997752"))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DETAIL_MAX_CHARS = 200


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


def derive_source(article: dict) -> str:
    """source が無い記事はURLのドメインから生成する。"""
    src = (article.get("source") or "").strip()
    if src:
        return src
    url = article.get("url", "")
    try:
        host = urlparse(url).hostname or ""
        return host.replace("www.", "")
    except Exception:
        return ""


def derive_detail(article: dict) -> str:
    """detail が無い場合は summary を結合してフォールバック。200字に切り詰め。"""
    detail = (article.get("detail") or "").strip()
    if not detail:
        summary = article.get("summary") or []
        if isinstance(summary, list):
            detail = " ".join(str(s).strip() for s in summary if s)
        else:
            detail = str(summary)
    if len(detail) > DETAIL_MAX_CHARS:
        detail = detail[: DETAIL_MAX_CHARS - 1] + "…"
    return detail


def build_rows(articles: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for a in articles:
        rows.append(
            [
                derive_source(a),
                (a.get("title") or "").strip(),
                (a.get("url") or "").strip(),
                derive_detail(a),
                (a.get("collected_at") or "").strip(),
            ]
        )
    return rows


def get_credentials() -> service_account.Credentials:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        print("Error: GOOGLE_SERVICE_ACCOUNT_JSON is not set", file=sys.stderr)
        sys.exit(1)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def resolve_sheet_name(service, spreadsheet_id: str, gid: int) -> str:
    """gid からタブ名を解決する。"""
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in metadata.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("sheetId") == gid:
            return props.get("title", "")
    raise RuntimeError(f"sheet with gid={gid} not found in spreadsheet {spreadsheet_id}")


def append_rows(service, spreadsheet_id: str, sheet_name: str, rows: list[list[str]]) -> int:
    body = {"values": rows}
    range_name = f"'{sheet_name}'!A:E"
    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        )
        .execute()
    )
    updates = result.get("updates", {})
    return int(updates.get("updatedRows", 0))


def main() -> None:
    articles = load_articles(ARTICLES_INPUT_PATH)
    rows = build_rows(articles)
    print(f"Prepared {len(rows)} rows for spreadsheet append")

    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet_name = resolve_sheet_name(service, SPREADSHEET_ID, TARGET_SHEET_GID)
        print(f"Target sheet: '{sheet_name}' (gid={TARGET_SHEET_GID})")
        appended = append_rows(service, SPREADSHEET_ID, sheet_name, rows)
        print(f"Appended {appended} rows to spreadsheet {SPREADSHEET_ID}")
    except Exception as e:
        print(f"Error: failed to append rows: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
