"""
fetch_kr.py — Korean insider buying tracker via DART OpenAPI

Fetches "임원·주요주주특정증권등소유상황보고서" (Form 4 equivalent for Korea)
filed today, filters for officer/director purchases >= 100M KRW,
and enriches with market cap + business description.

Usage:
    DART_API_KEY=xxxx python fetch_kr.py

Output: prints JSON list to stdout. Caller writes to disk.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import requests

DART_KEY = os.environ.get("DART_API_KEY", "").strip()
KST = timezone(timedelta(hours=9))

# DART filing report code: "B001" = 임원·주요주주특정증권등소유상황보고서
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_DOC_URL = "https://opendart.fss.or.kr/api/document.xml"
DART_COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"

# Officer/director job titles we care about (임원/이사 매수만)
OFFICER_TITLES = [
    "대표이사", "이사", "사내이사", "사외이사", "감사", "회장",
    "부회장", "사장", "부사장", "전무", "상무", "이사회의장",
    "CEO", "CFO", "COO", "CTO",
]

MIN_AMOUNT_KRW = 100_000_000  # 1억원


def log(msg: str) -> None:
    print(f"[fetch_kr] {msg}", file=sys.stderr)


def fetch_today_filings() -> list[dict]:
    """List today's insider holding reports (B001 = 임원·주요주주... 보고서)."""
    today = datetime.now(KST).strftime("%Y%m%d")
    params = {
        "crtfc_key": DART_KEY,
        "bgn_de": today,
        "end_de": today,
        "pblntf_detail_ty": "B001",  # detail filing type
        "page_count": 100,
    }
    r = requests.get(DART_LIST_URL, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("status") not in ("000", "013"):  # 013 = no results, treat as empty
        log(f"DART list error: {j.get('status')} {j.get('message')}")
        return []
    return j.get("list", []) or []


def fetch_company_info(corp_code: str) -> dict:
    """Fetch business description and stock ticker from DART company endpoint."""
    params = {"crtfc_key": DART_KEY, "corp_code": corp_code}
    try:
        r = requests.get(DART_COMPANY_URL, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "000":
            return {}
        return j
    except Exception as e:
        log(f"company info error for {corp_code}: {e}")
        return {}


def fetch_market_cap(ticker: str) -> int | None:
    """
    Best-effort market cap fetch.
    Uses Naver Finance public page (no auth) as a fallback that's reliable enough.
    For production, replace with KRX OpenAPI or a paid feed.
    """
    if not ticker or not re.fullmatch(r"\d{6}", ticker):
        return None
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        # 시가총액 패턴 — Naver renders like "시가총액<...>412조 1,234억원" or numeric form
        m = re.search(
            r"시가총액[\s\S]{0,500}?([\d,]+)조?\s*([\d,]*)억",
            r.text,
        )
        if not m:
            return None
        jo = m.group(1).replace(",", "")
        eok = (m.group(2) or "0").replace(",", "")
        if eok == "":
            eok = "0"
        # If first number looks like it's already 억 (no 조 component), Naver uses different pattern.
        # Try richer parse:
        m2 = re.search(r"시가총액[\s\S]{0,300}?<em[^>]*>([^<]+)</em>", r.text)
        if m2:
            txt = m2.group(1).replace(",", "").strip()
            # examples: "412조 1,234" or "1,234"
            if "조" in txt:
                parts = txt.split("조")
                jo_n = int(parts[0].strip())
                eok_n = int(parts[1].strip()) if parts[1].strip().isdigit() else 0
                return jo_n * 10**12 + eok_n * 10**8
            elif txt.isdigit():
                return int(txt) * 10**8  # 억 단위
        # Fallback: trust simple parse
        return int(jo) * 10**12 + int(eok) * 10**8 if jo.isdigit() else None
    except Exception as e:
        log(f"market cap error for {ticker}: {e}")
        return None


def parse_filing_detail(rcept_no: str) -> list[dict]:
    """
    Fetch the actual filing document and extract transaction rows.
    DART returns these as XBRL-ish XML. We parse for:
        - reporter name & role
        - transaction type (취득=acquire, 처분=dispose)
        - shares & price
        - acquire reason (장내매수 / 장외매수 / 증여 etc.)
    Only returns rows that are 장내매수 (open-market purchase) by officers/directors.
    """
    params = {"crtfc_key": DART_KEY, "rcept_no": rcept_no}
    try:
        r = requests.get(DART_DOC_URL, params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log(f"document fetch failed {rcept_no}: {e}")
        return []

    # Document is a ZIP containing XML; for OpenAPI it returns XML directly.
    # Different filings format inconsistently, so we do a permissive regex extract.
    text = r.text
    rows: list[dict] = []

    # Heuristic: each transaction row often appears in a table with columns like:
    # 보고자명 | 직위 | 변동일 | 변동수량 | 단가 | 변동사유
    # We look for blocks matching that pattern.
    block_re = re.compile(
        r"<TU[^>]*>(?P<role>[^<]*)</TU>[\s\S]{0,2000}?"
        r"<TU[^>]*>(?P<reason>[^<]*장내매수[^<]*)</TU>",
        re.IGNORECASE,
    )
    # Real-world parsing of DART filings is messy; production code should use the
    # dedicated detail endpoint /api/elestock.json for 임원·주요주주 data,
    # which returns clean structured JSON. We use that here:
    return []  # parse_filing_detail body intentionally minimal — superseded by elestock endpoint


def fetch_elestock(corp_code: str) -> list[dict]:
    """
    Fetch structured insider holding changes via DART's elestock endpoint.
    This is the clean API for 임원·주요주주특정증권등소유상황보고서 data.
    """
    url = "https://opendart.fss.or.kr/api/elestock.json"
    params = {"crtfc_key": DART_KEY, "corp_code": corp_code}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "000":
            return []
        return j.get("list", []) or []
    except Exception as e:
        log(f"elestock error {corp_code}: {e}")
        return []


def is_officer(role: str) -> bool:
    if not role:
        return False
    return any(t in role for t in OFFICER_TITLES)


def is_open_market_buy(reason: str) -> bool:
    """장내매수 = open-market purchase. We exclude 증여(gift), 행사(option exercise), etc."""
    if not reason:
        return False
    return "장내매수" in reason


def main() -> None:
    if not DART_KEY:
        log("ERROR: DART_API_KEY env var not set. Get one free at https://opendart.fss.or.kr")
        # Emit empty result so the pipeline doesn't crash
        print(json.dumps([], ensure_ascii=False))
        sys.exit(1)

    filings = fetch_today_filings()
    log(f"Found {len(filings)} insider filings today")

    # De-dupe by corp_code; we'll fetch elestock per corp once
    seen_corps: dict[str, dict] = {}
    for f in filings:
        cc = f.get("corp_code")
        if cc and cc not in seen_corps:
            seen_corps[cc] = f

    results: list[dict] = []

    # Group transactions by (corp, insider) so we can list each insider's
    # purchases as a list of transactions across multiple dates.
    # Key: (corp_code, insider_name, role) → aggregated entry
    grouped: dict[tuple, dict] = {}

    # Look back 7 days — DART can post filings several days after the trade
    today_dt = datetime.now(KST)
    today_iso = today_dt.strftime("%Y-%m-%d")
    cutoff = today_dt - timedelta(days=7)

    for corp_code, filing in seen_corps.items():
        time.sleep(0.3)  # be nice to DART
        rows = fetch_elestock(corp_code)
        if not rows:
            continue

        for row in rows:
            role = row.get("ofcps", "")
            reason = row.get("trde_rsn", "")
            tdate_raw = row.get("trde_de", "")
            tdate = re.sub(r"[^\d]", "-", tdate_raw)[:10]  # normalize to YYYY-MM-DD-ish

            if not is_officer(role):
                continue
            if not is_open_market_buy(reason):
                continue

            # Parse transaction date (best-effort). Skip if older than cutoff.
            try:
                tdate_dt = datetime.strptime(tdate[:10].replace("-", "")[:8], "%Y%m%d").replace(tzinfo=KST)
                if tdate_dt < cutoff:
                    continue
                tdate_iso = tdate_dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

            shares = int(str(row.get("trde_stock_qy", "0")).replace(",", "") or 0)
            price = int(str(row.get("trde_stock_unit_qy", "0")).replace(",", "") or 0)
            amount = shares * price

            if amount <= 0:
                continue

            insider_name = row.get("repror", "")
            key = (corp_code, insider_name, role)

            if key not in grouped:
                ticker = filing.get("stock_code") or row.get("stock_code", "")
                name = filing.get("corp_name", "")
                mcap = fetch_market_cap(ticker)
                company = fetch_company_info(corp_code)
                business = company.get("induty", "") or "—"

                grouped[key] = {
                    "ticker": ticker,
                    "name": name,
                    "market_cap_krw": mcap,
                    "business": business,
                    "insider_name": insider_name,
                    "insider_role": role,
                    "transactions": [],
                    "filing_date": today_iso,
                    "filing_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={filing.get('rcept_no', '')}",
                }

            grouped[key]["transactions"].append({
                "date": tdate_iso,
                "shares": shares,
                "price_per_share": price,
                "amount_krw": amount,
            })

    # Final filter: aggregate amount must clear MIN_AMOUNT_KRW threshold
    for entry in grouped.values():
        entry["transactions"].sort(key=lambda t: t["date"])
        total = sum(t["amount_krw"] for t in entry["transactions"])
        if total < MIN_AMOUNT_KRW:
            continue
        entry["transaction_amount_krw"] = total
        entry["shares"] = sum(t["shares"] for t in entry["transactions"])
        # Date range for display
        dates = [t["date"] for t in entry["transactions"]]
        entry["date_range"] = (
            dates[0] if dates[0] == dates[-1] else f"{dates[0]} ~ {dates[-1]}"
        )
        results.append(entry)

    log(f"Filtered to {len(results)} qualifying insiders ({sum(len(r['transactions']) for r in results)} transactions)")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
