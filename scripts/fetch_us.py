"""
fetch_us.py — US insider buying tracker via SEC EDGAR Form 4

Fetches Form 4 filings filed today, filters for officer/director purchases (P)
>= $100,000, and enriches with market cap + business description via yfinance.

Usage:
    SEC_USER_AGENT="Your Name your@email.com" python fetch_us.py

SEC requires a user-agent string with contact info per their fair-use policy.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests

ET_TZ = timezone(timedelta(hours=-4))  # US Eastern (DST-naive; close enough for filing dates)
USER_AGENT = os.environ.get("SEC_USER_AGENT", "InsiderTape research@example.com")
HEADERS = {"User-Agent": USER_AGENT}

EDGAR_BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

# Form 4 transaction codes — "P" = open market or private purchase
PURCHASE_CODES = {"P"}

# Officer/director titles we accept
# Form 4 has structured fields: isOfficer, isDirector, officerTitle
MIN_AMOUNT_USD = 100_000


def log(msg: str) -> None:
    print(f"[fetch_us] {msg}", file=sys.stderr)


def fetch_recent_form4_filings() -> list[dict]:
    """
    Use EDGAR full-text search RSS to get today's Form 4 filings.
    EDGAR provides a daily index that's more reliable than search:
    https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{N}/form.{YYYYMMDD}.idx
    """
    today = datetime.now(ET_TZ)
    yyyy = today.year
    qtr = (today.month - 1) // 3 + 1
    yyyymmdd = today.strftime("%Y%m%d")
    idx_url = (
        f"https://www.sec.gov/Archives/edgar/daily-index/"
        f"{yyyy}/QTR{qtr}/form.{yyyymmdd}.idx"
    )
    try:
        r = requests.get(idx_url, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            log(f"No daily index yet for {yyyymmdd} (market may be closed)")
            return []
        r.raise_for_status()
    except Exception as e:
        log(f"daily-index fetch failed: {e}")
        return []

    filings: list[dict] = []
    for line in r.text.splitlines():
        # Format: form_type, company, cik, date_filed, file_path
        # Whitespace-separated, but company name contains spaces.
        if not line.startswith("4 "):
            continue
        # Use a regex: form_type (with optional /A) starts at col 0, spaces, then fields
        m = re.match(
            r"^(?P<form>4(?:/A)?)\s+(?P<name>.+?)\s+(?P<cik>\d{1,10})\s+"
            r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<path>edgar/\S+)",
            line,
        )
        if not m:
            continue
        filings.append({
            "form": m.group("form"),
            "company": m.group("name").strip(),
            "cik": m.group("cik").lstrip("0") or "0",
            "date": m.group("date"),
            "path": m.group("path"),
            "url": f"https://www.sec.gov/Archives/{m.group('path')}",
        })
    return filings


def fetch_form4_xml(filing: dict) -> str | None:
    """
    The .txt index points to a submission. We need the actual XML.
    Replace -index.htm with the directory listing, find the .xml.
    """
    base = filing["url"].rsplit("/", 1)[0] + "/"
    try:
        r = requests.get(base, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log(f"index listing failed {base}: {e}")
        return None

    # Find first .xml file (Form 4 primary doc)
    xml_files = re.findall(r'href="([^"]+\.xml)"', r.text)
    # Skip XBRL companion files; primary is usually first or named like wf-form4_*
    primary = next(
        (x for x in xml_files if "form4" in x.lower() or x.startswith("wf-")),
        xml_files[0] if xml_files else None,
    )
    if not primary:
        return None
    xml_url = base + primary if not primary.startswith("http") else primary
    try:
        r = requests.get(xml_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log(f"xml fetch failed {xml_url}: {e}")
        return None


def parse_form4(xml_text: str, filing: dict) -> list[dict]:
    """
    Extract purchase transactions from Form 4 XML.

    Returns one entry per (issuer, reporter) combination, with a list of
    individual transactions across dates. A single Form 4 may contain
    multiple transactions on different dates.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"xml parse error: {e}")
        return []

    # Reporter info
    reporter_name = (
        root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or ""
    ).strip()
    is_officer = (
        root.findtext(".//reportingOwner/reportingOwnerRelationship/isOfficer") or "0"
    ).strip() in ("1", "true")
    is_director = (
        root.findtext(".//reportingOwner/reportingOwnerRelationship/isDirector") or "0"
    ).strip() in ("1", "true")
    officer_title = (
        root.findtext(".//reportingOwner/reportingOwnerRelationship/officerTitle") or ""
    ).strip()

    if not (is_officer or is_director):
        return []

    role = officer_title or ("Director" if is_director else "Officer")
    issuer_name = (root.findtext(".//issuer/issuerName") or filing["company"]).strip()
    ticker = (root.findtext(".//issuer/issuerTradingSymbol") or "").strip()

    # Collect all qualifying transactions in this filing
    txns: list[dict] = []
    for txn in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = (txn.findtext(".//transactionCoding/transactionCode") or "").strip()
        if code not in PURCHASE_CODES:
            continue
        ad = (
            txn.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
            or ""
        ).strip()
        if ad != "A":
            continue
        try:
            shares = float(
                txn.findtext(".//transactionAmounts/transactionShares/value") or 0
            )
            price = float(
                txn.findtext(".//transactionAmounts/transactionPricePerShare/value") or 0
            )
        except (TypeError, ValueError):
            continue
        if price <= 0 or shares <= 0:
            continue
        amount = shares * price
        txn_date = (
            txn.findtext(".//transactionDate/value") or filing["date"]
        ).strip()

        txns.append({
            "date": txn_date,
            "shares": int(shares),
            "price_per_share": round(price, 2),
            "amount_usd": round(amount, 2),
        })

    if not txns:
        return []

    txns.sort(key=lambda t: t["date"])
    total = sum(t["amount_usd"] for t in txns)
    if total < MIN_AMOUNT_USD:
        return []

    dates = [t["date"] for t in txns]
    date_range = dates[0] if dates[0] == dates[-1] else f"{dates[0]} ~ {dates[-1]}"

    return [{
        "ticker": ticker,
        "name": issuer_name,
        "insider_name": reporter_name,
        "insider_role": role,
        "shares": sum(t["shares"] for t in txns),
        "transaction_amount_usd": round(total, 2),
        "transactions": txns,
        "date_range": date_range,
        "filing_date": filing["date"],
        "filing_url": filing["url"].replace(".txt", "-index.htm"),
    }]


def enrich_with_yfinance(items: list[dict]) -> list[dict]:
    """Add market_cap_usd and business description per ticker."""
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance not installed; skipping enrichment")
        for x in items:
            x["market_cap_usd"] = None
            x["business"] = "—"
        return items

    # Cache by ticker
    cache: dict[str, dict] = {}
    for x in items:
        t = x["ticker"]
        if not t:
            x["market_cap_usd"] = None
            x["business"] = "—"
            continue
        if t not in cache:
            try:
                info = yf.Ticker(t).info or {}
                cache[t] = {
                    "market_cap_usd": info.get("marketCap"),
                    "business": (
                        info.get("longBusinessSummary", "")[:160].strip() + "…"
                        if info.get("longBusinessSummary")
                        else info.get("industry") or "—"
                    ),
                }
            except Exception as e:
                log(f"yfinance error for {t}: {e}")
                cache[t] = {"market_cap_usd": None, "business": "—"}
        x["market_cap_usd"] = cache[t]["market_cap_usd"]
        x["business"] = cache[t]["business"]
    return items


def main() -> None:
    filings = fetch_recent_form4_filings()
    log(f"Found {len(filings)} Form 4 filings today")

    all_txns: list[dict] = []
    for f in filings:
        time.sleep(0.12)  # SEC fair-use: 10 req/sec max
        xml = fetch_form4_xml(f)
        if not xml:
            continue
        all_txns.extend(parse_form4(xml, f))

    log(f"Found {len(all_txns)} qualifying officer/director purchases")
    enriched = enrich_with_yfinance(all_txns)
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
