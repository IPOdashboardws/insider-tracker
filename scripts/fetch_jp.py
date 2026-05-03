"""
fetch_jp.py — Japanese 5%+ shareholder accumulation tracker via EDINET API

EDINET (Electronic Disclosure for Investors' NETwork) is Japan's FSA filing system,
analogous to Korea's DART. We track 大量保有報告書 (Large-Volume Holding Reports)
which are required when an investor crosses 5% ownership.

This is NOT a perfect insider-buying tracker for Japan because:
- Officers buying < 5% of float are not captured
- Reports cover any holder, not just officers/directors
- Filing window is up to 5 business days after the trigger event

But it does capture: founders, CEOs, and major shareholders adding to positions,
which is often the highest-signal subset of insider activity anyway.

Usage:
    EDINET_API_KEY=xxxx python fetch_jp.py

Get a free API key at https://api.edinet-fsa.go.jp (registration required as of 2024).
Output: prints JSON list to stdout.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import zipfile
import io
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests

JST = timezone(timedelta(hours=9))
EDINET_KEY = os.environ.get("EDINET_API_KEY", "").strip()

EDINET_LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
EDINET_DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"

# Document type codes for 大量保有報告書 family
# 040: 大量保有報告書 (initial)
# 050: 変更報告書 (amendment - includes additional purchases)
LARGE_HOLDING_DOC_TYPES = {"040", "050"}

# Minimum acquisition value to report (¥100M ≈ ~$650K, comparable to KR/US thresholds)
MIN_AMOUNT_JPY = 100_000_000

# yfinance suffix for Japanese tickers
YF_SUFFIX = ".T"


def log(msg: str) -> None:
    print(f"[fetch_jp] {msg}", file=sys.stderr)


def fetch_today_filings() -> list[dict]:
    """List today's EDINET filings, filter to large-holding reports."""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    params = {
        "date": today,
        "type": 2,  # 2 = list with metadata
        "Subscription-Key": EDINET_KEY,
    }
    try:
        r = requests.get(EDINET_LIST_URL, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log(f"EDINET list error: {e}")
        return []

    if j.get("metadata", {}).get("status") != "200":
        log(f"EDINET status: {j.get('metadata', {})}")
        return []

    results = j.get("results", []) or []
    # Filter to large-holding reports
    filings = [
        d for d in results
        if d.get("docTypeCode") in LARGE_HOLDING_DOC_TYPES
    ]
    return filings


def fetch_document_xbrl(doc_id: str) -> str | None:
    """Download a filing as XBRL-in-ZIP and extract the main XBRL XML."""
    url = EDINET_DOC_URL.format(doc_id=doc_id)
    params = {"type": 1, "Subscription-Key": EDINET_KEY}  # type=1: XBRL zip
    try:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
    except Exception as e:
        log(f"doc fetch failed {doc_id}: {e}")
        return None

    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        log(f"bad zip {doc_id}")
        return None

    # Find the main XBRL file (usually in PublicDoc/, with extension .xbrl)
    xbrl_files = [
        n for n in z.namelist()
        if n.lower().endswith(".xbrl") and "publicdoc" in n.lower()
    ]
    if not xbrl_files:
        xbrl_files = [n for n in z.namelist() if n.lower().endswith(".xbrl")]
    if not xbrl_files:
        return None

    return z.read(xbrl_files[0]).decode("utf-8", errors="ignore")


def parse_large_holding(xbrl_text: str, filing: dict) -> list[dict]:
    """
    Parse 大量保有報告書 XBRL.

    Key fields we want (EDINET XBRL element local names, namespace varies):
    - 発行者の名称 (issuer name)
    - 銘柄コード (ticker, 4-digit numeric)
    - 提出者の氏名又は名称 (filer name)
    - 保有株券等の数 (current shares held)
    - 保有株券等の数 - 直前 (previous shares held, on amendments)
    - 保有株券等の数の増減 (change in shares)
    - 取得資金の総額 / 取得・処分の対価 (acquisition cost)
    - 株券等保有割合 (current ownership %)
    - 株券等保有割合 - 直前 (previous ownership %)
    """
    try:
        root = ET.fromstring(xbrl_text)
    except ET.ParseError as e:
        log(f"xbrl parse error: {e}")
        return []

    # XBRL elements use namespaces. We strip them and search by local name.
    def find_text(local_names: list[str]) -> str:
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag in local_names and el.text:
                return el.text.strip()
        return ""

    def find_all_text(local_names: list[str]) -> list[str]:
        out = []
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag in local_names and el.text:
                out.append(el.text.strip())
        return out

    issuer_name = find_text([
        "FilerNameInJapaneseDEI",
        "IssuerNameCoverPage",
        "NameOfIssuerCoverPage",
    ]) or filing.get("filerName", "")

    ticker_raw = find_text([
        "SecurityCodeDEI",
        "SecurityCodeOfIssuerCoverPage",
        "SecurityCode",
    ])
    ticker = re.sub(r"[^0-9]", "", ticker_raw)[:4] if ticker_raw else ""

    filer_name = find_text([
        "FilerNameInJapaneseCoverPage",
        "NameCoverPage",
        "Name",
    ]) or filing.get("filerName", "")

    # Ownership % current and prior
    pct_current_str = find_text([
        "TotalHoldingRatioOfShareCertificatesEtc",
        "HoldingRatioOfShareCertificatesEtc",
    ])
    pct_prior_str = find_text([
        "TotalHoldingRatioOfShareCertificatesEtcOfLastReport",
        "HoldingRatioOfShareCertificatesEtcOfLastReport",
    ])

    def to_float(s: str) -> float | None:
        if not s:
            return None
        try:
            return float(s.replace(",", "").replace("%", ""))
        except ValueError:
            return None

    pct_current = to_float(pct_current_str)
    pct_prior = to_float(pct_prior_str)

    # Determine if this is an accumulation (acquisition) vs disposition
    # On initial 040 filings, prior is usually empty/0 → it's a new 5%+ stake.
    # On 050 amendments, compare current vs prior.
    is_acquisition: bool
    if filing.get("docTypeCode") == "040":
        is_acquisition = True  # initial 5% disclosure = they crossed up
    else:
        if pct_current is None or pct_prior is None:
            return []  # can't determine direction
        is_acquisition = pct_current > pct_prior

    if not is_acquisition:
        return []

    # Acquisition cost — try several possible XBRL element names
    cost_str = find_text([
        "TotalConsiderationOfAcquisition",
        "ConsiderationOfTransaction",
        "AcquisitionFundOfShareCertificatesEtc",
    ])
    amount = to_float(cost_str)
    if amount is not None:
        amount = int(amount)

    # If cost not directly reported, try to compute from shares × price (often missing in this filing type)
    if amount is None:
        # Fall back: shares delta × recent price would need a separate lookup.
        # For now, mark as None and we'll filter these out unless % jump is significant.
        pass

    # Shares info (for display)
    shares_current_str = find_text([
        "TotalNumberOfSharesEtcHeld",
        "NumberOfSharesEtcHeld",
    ])
    shares = None
    try:
        shares = int(shares_current_str.replace(",", "")) if shares_current_str else None
    except ValueError:
        shares = None

    # Apply minimum-amount filter (skip if we can't determine amount and pct change is small)
    # Try to extract per-date transactions from "直近60日間の取得・処分の状況" section.
    # XBRL element names vary; we search heuristically for date+amount pairs.
    transactions = extract_recent_transactions(root)

    if amount is None:
        if pct_prior is not None and pct_current is not None and (pct_current - pct_prior) < 1.0:
            return []  # Small adjustment, no cost data → skip
    elif amount < MIN_AMOUNT_JPY:
        return []

    return [{
        "ticker": ticker,
        "name": issuer_name,
        "filer_name": filer_name,
        "pct_current": pct_current,
        "pct_prior": pct_prior,
        "shares": shares,
        "transaction_amount_jpy": amount,
        "transactions": transactions,
        "doc_type": filing.get("docTypeCode"),
        "doc_id": filing.get("docID"),
        "filing_date": filing.get("submitDateTime", "")[:10],
    }]


def extract_recent_transactions(root) -> list[dict]:
    """
    Try to extract per-date transaction list from the "直近60日間の取得・処分の状況"
    table in the XBRL. This data is structured as a context-grouped list where
    each transaction has: 年月日 (date), 株券等の種類 (type), 数量 (qty), 単価 (price).

    Returns a list of {date, shares, price_per_share, amount_jpy} sorted by date.
    Returns empty list if extraction fails — caller falls back to aggregate display.
    """
    # XBRL contexts are id'd; same context id ties date/qty/price for one txn row.
    # We collect all elements, group by their parent context, then pull pairs.
    txns_by_context: dict[str, dict] = {}

    relevant_locals = {
        "DateOfTransactionOfRecentSixtyDays": "date",
        "TransactionDateOfRecentSixtyDaysOfShareCertificatesEtc": "date",
        "AcquisitionOrDisposalDate": "date",
        "NumberOfTransactionOfRecentSixtyDays": "shares",
        "NumberOfTransactionOfRecentSixtyDaysOfShareCertificatesEtc": "shares",
        "TransactionVolume": "shares",
        "PricePerUnitOfTransactionOfRecentSixtyDays": "price",
        "UnitPrice": "price",
        "TransactionPricePerUnit": "price",
    }

    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag not in relevant_locals:
            continue
        ctx = el.attrib.get("contextRef", "")
        if not ctx:
            continue
        if ctx not in txns_by_context:
            txns_by_context[ctx] = {}
        field = relevant_locals[tag]
        text = (el.text or "").strip()
        if text:
            txns_by_context[ctx][field] = text

    out: list[dict] = []
    for ctx, parts in txns_by_context.items():
        date_s = parts.get("date")
        shares_s = parts.get("shares")
        price_s = parts.get("price")
        if not (date_s and shares_s):
            continue
        # Normalize date to YYYY-MM-DD
        date_clean = re.sub(r"[年月]", "-", date_s).replace("日", "").strip()
        date_clean = re.sub(r"[^\d-]", "", date_clean).strip("-")
        try:
            parts_dt = [int(p) for p in date_clean.split("-") if p]
            if len(parts_dt) >= 3:
                date_iso = f"{parts_dt[0]:04d}-{parts_dt[1]:02d}-{parts_dt[2]:02d}"
            else:
                continue
        except ValueError:
            continue

        try:
            shares = int(float(shares_s.replace(",", "")))
            price = float(price_s.replace(",", "")) if price_s else 0.0
        except ValueError:
            continue

        # Only acquisitions (positive shares); skip if shares <= 0
        if shares <= 0:
            continue

        out.append({
            "date": date_iso,
            "shares": shares,
            "price_per_share": round(price, 2) if price else None,
            "amount_jpy": int(shares * price) if price else None,
        })

    out.sort(key=lambda t: t["date"])
    return out


def enrich_with_yfinance(items: list[dict]) -> list[dict]:
    """Add market cap and business description via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance not installed; skipping enrichment")
        for x in items:
            x["market_cap_jpy"] = None
            x["business"] = "—"
        return items

    cache: dict[str, dict] = {}
    for x in items:
        t = x["ticker"]
        if not t:
            x["market_cap_jpy"] = None
            x["business"] = "—"
            continue
        yf_ticker = t + YF_SUFFIX
        if yf_ticker not in cache:
            try:
                info = yf.Ticker(yf_ticker).info or {}
                # yfinance returns market cap in JPY for .T tickers
                cache[yf_ticker] = {
                    "market_cap_jpy": info.get("marketCap"),
                    "business": (
                        info.get("longBusinessSummary", "")[:160].strip() + "…"
                        if info.get("longBusinessSummary")
                        else info.get("industry") or "—"
                    ),
                }
            except Exception as e:
                log(f"yfinance error for {yf_ticker}: {e}")
                cache[yf_ticker] = {"market_cap_jpy": None, "business": "—"}
        x["market_cap_jpy"] = cache[yf_ticker]["market_cap_jpy"]
        x["business"] = cache[yf_ticker]["business"]

        # Add filing URL (EDINET viewer)
        x["filing_url"] = (
            f"https://disclosure2.edinet-fsa.go.jp/WEEK0010.aspx?"
            f"submitDocumentId={x.get('doc_id', '')}"
        )
    return items


def main() -> None:
    if not EDINET_KEY:
        log("ERROR: EDINET_API_KEY env var not set. Register at https://api.edinet-fsa.go.jp")
        print(json.dumps([], ensure_ascii=False))
        sys.exit(1)

    filings = fetch_today_filings()
    log(f"Found {len(filings)} large-holding reports today")

    all_results: list[dict] = []
    for f in filings:
        time.sleep(0.3)  # be nice to EDINET
        xbrl = fetch_document_xbrl(f.get("docID", ""))
        if not xbrl:
            continue
        all_results.extend(parse_large_holding(xbrl, f))

    log(f"Filtered to {len(all_results)} qualifying acquisitions")
    enriched = enrich_with_yfinance(all_results)
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
