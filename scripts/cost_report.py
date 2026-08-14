#!/usr/bin/env python3
"""
週次コストレポート

Anthropic Admin API と GCP BigQuery Billing Export から
当月(MTD)の概算費用と出力量を取得し、Google スプレッドシートの
「Cost」タブに 1 行ずつ追記する。

必要な環境変数:
  ANTHROPIC_ADMIN_KEY          Anthropic Admin API キー (sk-ant-admin-...)
  GOOGLE_SERVICE_ACCOUNT_JSON  Sheets/BigQuery 用サービスアカウント JSON
  GCP_BILLING_EXPORT_TABLE     BigQuery 課金エクスポートテーブル
                               形式: project.dataset.table

いずれかが未設定/失敗しても他方の集計と追記は続行する
(該当行の備考に理由を記録)。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1ih5hFlS8kwGgcSP_cau8b0VmYcWtfVjP3pml5RrpRe4"
)
COST_SHEET_TITLE = os.environ.get("COST_SHEET_TITLE", "Cost")

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/bigquery.readonly",
]

ANTHROPIC_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT = 30

JST = timezone(timedelta(hours=9))

HEADER_ROW = [
    "取得日時 (JST)",
    "期間",
    "サービス",
    "出力量",
    "単位",
    "概算費用 (USD)",
    "備考",
]


def month_range_utc(now_utc: datetime) -> tuple[datetime, datetime]:
    """当月初 (UTC 00:00) から now までの範囲を返す。"""
    start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now_utc


def rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_anthropic_cost_and_output(
    admin_key: str, start_utc: datetime, end_utc: datetime
) -> tuple[float, int, str]:
    """Anthropic Admin API から当月の総コスト(USD)と総 output_tokens を返す。

    Returns: (cost_usd, output_tokens, note)
    """
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    base_params = {
        "starting_at": rfc3339(start_utc),
        "ending_at": rfc3339(end_utc),
        "bucket_width": "1d",
    }

    total_cost = 0.0
    total_output = 0

    def _paginate(path: str, extract):
        page_token = None
        for _ in range(20):  # safety cap
            params = dict(base_params)
            if page_token:
                params["page"] = page_token
            resp = requests.get(
                f"{ANTHROPIC_BASE}{path}",
                headers=headers,
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            extract(payload)
            if not payload.get("has_more"):
                return
            page_token = payload.get("next_page")
            if not page_token:
                return

    def _extract_cost(payload: dict) -> None:
        nonlocal total_cost
        for bucket in payload.get("data", []):
            for entry in bucket.get("results", []):
                # フィールド名はバージョンで揺れがあるので順に試す
                amount = (
                    entry.get("amount")
                    or entry.get("cost")
                    or entry.get("cost_amount")
                    or 0
                )
                total_cost += _to_float(amount)

    def _extract_usage(payload: dict) -> None:
        nonlocal total_output
        for bucket in payload.get("data", []):
            for entry in bucket.get("results", []):
                total_output += int(
                    _to_float(
                        entry.get("output_tokens")
                        or entry.get("output_token_count")
                        or 0
                    )
                )

    _paginate("/v1/organizations/cost_report", _extract_cost)
    _paginate("/v1/organizations/usage_report/messages", _extract_usage)

    return total_cost, total_output, ""


def fetch_gcp_cost_and_usage(
    creds: service_account.Credentials,
    table: str,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[float, float, str]:
    """BigQuery 課金エクスポートから当月コストと usage.amount 合計を返す。

    Returns: (cost_usd, usage_amount, note)
    """
    try:
        from google.cloud import bigquery
    except ImportError as e:
        return 0.0, 0.0, f"google-cloud-bigquery not installed: {e}"

    parts = table.split(".")
    if len(parts) != 3:
        return 0.0, 0.0, f"GCP_BILLING_EXPORT_TABLE must be 'project.dataset.table' (got: {table})"
    project = parts[0]

    client = bigquery.Client(project=project, credentials=creds)
    query = f"""
      SELECT
        COALESCE(SUM(cost), 0)                              AS cost_usd,
        COALESCE(SUM(CAST(usage.amount AS FLOAT64)), 0)     AS usage_amount,
        STRING_AGG(DISTINCT service.description, ', ' LIMIT 3) AS top_services
      FROM `{table}`
      WHERE usage_start_time >= @start_ts
        AND usage_start_time <  @end_ts
        AND currency = 'USD'
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_ts", "TIMESTAMP", start_utc),
            bigquery.ScalarQueryParameter("end_ts", "TIMESTAMP", end_utc),
        ]
    )
    rows = list(client.query(query, job_config=job_config).result())
    if not rows:
        return 0.0, 0.0, "no rows"
    r = rows[0]
    note = f"top: {r['top_services']}" if r["top_services"] else ""
    return _to_float(r["cost_usd"]), _to_float(r["usage_amount"]), note


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
    return service_account.Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)


def ensure_cost_sheet(service, spreadsheet_id: str, title: str) -> None:
    """タブが無ければ作成し、空ならヘッダ行を書き込む。"""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    exists = any(
        s.get("properties", {}).get("title") == title for s in meta.get("sheets", [])
    )
    if not exists:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        print(f"Created new sheet tab: '{title}'")

    got = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{title}'!A1:G1")
        .execute()
    )
    if not got.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [HEADER_ROW]},
        ).execute()
        print("Wrote header row")


def append_rows(service, spreadsheet_id: str, title: str, rows: list[list[str]]) -> int:
    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=f"'{title}'!A:G",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        )
        .execute()
    )
    return int(result.get("updates", {}).get("updatedRows", 0))


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    start_utc, end_utc = month_range_utc(now_utc)
    period_label = f"{now_jst.strftime('%Y-%m')} (MTD)"
    collected_at = now_jst.strftime("%Y-%m-%d %H:%M")

    rows: list[list[str]] = []

    # ---- Anthropic ----
    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
    if not admin_key:
        rows.append(
            [collected_at, period_label, "Anthropic (Claude)", "", "output_tokens", "",
             "ANTHROPIC_ADMIN_KEY 未設定"]
        )
    else:
        try:
            cost, out_tokens, note = fetch_anthropic_cost_and_output(
                admin_key, start_utc, end_utc
            )
            rows.append(
                [collected_at, period_label, "Anthropic (Claude)",
                 f"{out_tokens:,}", "output_tokens",
                 f"{cost:.2f}", note or "-"]
            )
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            rows.append(
                [collected_at, period_label, "Anthropic (Claude)", "", "output_tokens", "",
                 f"取得失敗: {type(e).__name__}: {e}"]
            )

    # ---- Google Cloud ----
    bq_table = os.environ.get("GCP_BILLING_EXPORT_TABLE", "").strip()
    creds = get_credentials()
    if not bq_table:
        rows.append(
            [collected_at, period_label, "Google Cloud", "", "usage_units", "",
             "GCP_BILLING_EXPORT_TABLE 未設定 (BigQuery 課金エクスポート要)"]
        )
    else:
        try:
            gcp_cost, gcp_usage, note = fetch_gcp_cost_and_usage(
                creds, bq_table, start_utc, end_utc
            )
            rows.append(
                [collected_at, period_label, "Google Cloud",
                 f"{gcp_usage:,.0f}", "usage_units",
                 f"{gcp_cost:.2f}", note or "-"]
            )
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            rows.append(
                [collected_at, period_label, "Google Cloud", "", "usage_units", "",
                 f"BQ 取得失敗: {type(e).__name__}: {e}"]
            )

    for r in rows:
        print(" | ".join(str(c) for c in r))

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    ensure_cost_sheet(service, SPREADSHEET_ID, COST_SHEET_TITLE)
    appended = append_rows(service, SPREADSHEET_ID, COST_SHEET_TITLE, rows)
    print(f"Appended {appended} row(s) to '{COST_SHEET_TITLE}' tab")


if __name__ == "__main__":
    main()
