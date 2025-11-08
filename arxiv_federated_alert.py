#!/usr/bin/env python3
"""
arxiv_federated_alert.py

Polls the arXiv API for new papers on "federated learning" (optionally within specific categories),
filters results by keywords, deduplicates using a local state file, and sends a daily digest
via SMTP email and/or Slack webhook.

- Pure standard library (no external dependencies).
- Designed to be run via cron (e.g., once per day).
- Stores state in "arxiv_state.json" next to the script by default.
- Supports simple keyword AND / OR filtering on title+abstract.

Usage:
    python arxiv_federated_alert.py

Configuration:
    Either export environment variables or copy .env.example to .env and edit.
    Environment variables (all optional unless emailing/slack is enabled):
        # Query / filtering
        ARXIV_CATEGORIES="cs.LG,cs.DC,stat.ML"
        ARXIV_MAIN_QUERY='(ti:"federated learning" OR abs:"federated learning" OR all:federated)'
        ARXIV_REQUIRED_KEYWORDS="time series,temporal,sequential"
        ARXIV_ANY_KEYWORDS="privacy,convergence,generalization,industrial,forecasting"
        ARXIV_DAYS_BACK="7"                # window of interest if no state yet
        ARXIV_MAX_RESULTS="200"            # API upper bound per run (<=30000 hard cap, but keep modest)

        # Output
        DIGEST_MAX_ITEMS="50"              # cap items in an email/slack message
        SHOW_ABSTRACT="1"                  # "1" to include short abstract snippets in the digest

        # SMTP email (optional)
        SMTP_HOST=
        SMTP_PORT=587
        SMTP_USER=
        SMTP_PASS=
        SMTP_FROM=
        SMTP_TO=you@example.com,you2@example.com

        # Slack webhook (optional)
        SLACK_WEBHOOK_URL=

        # State file (optional override)
        STATE_FILE="arxiv_state.json"

Cron example (runs daily at 08:10 Europe/Paris):
    10 8 * * * /usr/bin/env -i bash -lc 'cd /path/to && export $(grep -v '^#' .env | xargs) && /usr/bin/python3 arxiv_federated_alert.py'

Note:
- arXiv API: https://info.arxiv.org/help/api/user-manual.html
- Be polite with rate limits. This script makes a single request per run by default.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import smtplib
import ssl
import sys
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional

ARXIV_API_URL = "https://export.arxiv.org/api/query"  # official endpoint

def _getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name, default)
    return val if (val is not None and val != "") else default

def load_env_dotenv_if_present(path: str = ".env") -> None:
    """Load key=value lines from a local .env (no quotes or spaces around equals)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            # Don't override if already in env
            os.environ.setdefault(k.strip(), v.strip())

def parse_csv_env(name: str) -> List[str]:
    raw = _getenv(name)
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]

def build_search_query(categories: List[str], main_query: str) -> str:
    """
    Build arXiv search_query.
    Example:
      categories=['cs.LG','cs.DC'] and main_query='(ti:"federated learning" OR abs:"federated learning")'
      => '(cat:cs.LG OR cat:cs.DC) AND (ti:"federated learning" OR abs:"federated learning")'
    """
    cats = " OR ".join([f"cat:{c}" for c in categories]) if categories else ""
    if cats and main_query:
        return f"({cats}) AND {main_query}"
    return main_query or cats

def arxiv_query(search_query: str, max_results: int = 200, sort_by: str = "submittedDate", sort_order: str = "descending") -> str:
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "arxiv-federated-alert/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")

def parse_atom(atom_xml: str) -> List[Dict[str, str]]:
    """
    Returns a list of dicts with keys: id, title, summary, updated, published, authors (comma string), link
    """
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(atom_xml)

    entries = []
    for e in root.findall("atom:entry", ns):
        arxiv_id = (e.findtext("atom:id", default="", namespaces=ns) or "").strip()
        title = (e.findtext("atom:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (e.findtext("atom:summary", default="", namespaces=ns) or "").strip().replace("\n", " ")
        updated = (e.findtext("atom:updated", default="", namespaces=ns) or "").strip()
        published = (e.findtext("atom:published", default="", namespaces=ns) or "").strip()

        # Authors
        authors = []
        for a in e.findall("atom:author", ns):
            name = (a.findtext("atom:name", default="", namespaces=ns) or "").strip()
            if name:
                authors.append(name)
        authors_str = ", ".join(authors)

        # Link to abs page
        link = ""
        for l in e.findall("atom:link", ns):
            if l.attrib.get("rel") == "alternate":
                link = l.attrib.get("href", "")
                break

        entries.append({
            "id": arxiv_id,
            "title": title,
            "summary": summary,
            "updated": updated,
            "published": published,
            "authors": authors_str,
            "link": link,
        })
    return entries

def parse_rfc3339(ts: str) -> dt.datetime:
    # arXiv uses RFC3339 timestamps; use email.utils for robustness, then ensure naive UTC
    dt_parsed = email.utils.parsedate_to_datetime(ts)
    if dt_parsed.tzinfo is not None:
        dt_parsed = dt_parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return dt_parsed

def load_state(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(path: str, state: Dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def filter_entries(entries: List[Dict[str, str]], required_all: List[str], any_keywords: List[str]) -> List[Dict[str, str]]:
    """
    Keep entries whose (title+summary) contains ALL tokens in required_all (case-insensitive),
    and ALSO contains at least one token in any_keywords if any_keywords is non-empty.
    """
    out = []
    for e in entries:
        hay = f"{e['title']} {e['summary']}".lower()
        ok_all = all(tok.lower() in hay for tok in required_all) if required_all else True
        ok_any = any(tok.lower() in hay for tok in any_keywords) if any_keywords else True
        if ok_all and ok_any:
            out.append(e)
    return out

def entries_newer_than(entries: List[Dict[str, str]], cutoff: dt.datetime) -> List[Dict[str, str]]:
    out = []
    for e in entries:
        pub = parse_rfc3339(e["published"]) if e["published"] else None
        upd = parse_rfc3339(e["updated"]) if e["updated"] else None
        t = max([x for x in [pub, upd] if x is not None], default=None)
        if t and t >= cutoff:
            out.append(e)
    return out

def dedupe_by_id(entries: List[Dict[str, str]], seen_ids: set[str]) -> List[Dict[str, str]]:
    out = []
    for e in entries:
        arxiv_id = e["id"]
        if arxiv_id not in seen_ids:
            out.append(e)
    return out

def format_digest(entries: List[Dict[str, str]], include_abstract: bool = True, max_items: int = 50) -> str:
    items = entries[:max_items]
    lines = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"arXiv federated learning digest — {now}\n")
    for i, e in enumerate(items, 1):
        title = e['title'].strip()
        authors = e['authors']
        link = e['link'] or e['id']
        published = e['published'][:10] if e['published'] else ""
        lines.append(f"{i}. {title} ({published})")
        if authors:
            lines.append(f"   {authors}")
        lines.append(f"   {link}")
        if include_abstract and e['summary']:
            abstract = textwrap.shorten(e['summary'], width=600, placeholder=" ...")
            lines.append(f"   Abstract: {abstract}")
        lines.append("")  # blank line
    if len(entries) > max_items:
        lines.append(f"(+{len(entries)-max_items} more omitted)")
    return "\n".join(lines).strip()

def send_email_smtp(subject: str, body: str) -> None:
    host = _getenv("SMTP_HOST")
    port = int(_getenv("SMTP_PORT", "587"))
    user = _getenv("SMTP_USER")
    password = _getenv("SMTP_PASS")
    from_addr = _getenv("SMTP_FROM", user or "")
    to_addrs = parse_csv_env("SMTP_TO")

    if not (host and port and from_addr and to_addrs):
        print("Email not sent: SMTP configuration incomplete.", file=sys.stderr)
        return

    msg = f"From: {from_addr}\r\nTo: {', '.join(to_addrs)}\r\nSubject: {subject}\r\n\r\n{body}"
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, to_addrs, msg.encode("utf-8"))
    print(f"Email sent to: {', '.join(to_addrs)}")

def send_slack_webhook(text: str) -> None:
    url = _getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        print("Posted to Slack webhook.")
    except Exception as e:
        print(f"Slack webhook failed: {e}", file=sys.stderr)

def main() -> int:
    load_env_dotenv_if_present(".env")

    categories = parse_csv_env("ARXIV_CATEGORIES") or ["cs.LG", "cs.DC", "stat.ML"]
    main_query = _getenv("ARXIV_MAIN_QUERY", '(ti:"federated learning" OR abs:"federated learning" OR all:federated)')
    search_query = build_search_query(categories, main_query)

    max_results = int(_getenv("ARXIV_MAX_RESULTS", "200"))
    days_back = int(_getenv("ARXIV_DAYS_BACK", "7"))
    digest_max = int(_getenv("DIGEST_MAX_ITEMS", "50"))
    show_abs = _getenv("SHOW_ABSTRACT", "1") == "1"

    required_all = parse_csv_env("ARXIV_REQUIRED_KEYWORDS")
    any_keywords = parse_csv_env("ARXIV_ANY_KEYWORDS")

    state_file = _getenv("STATE_FILE", "arxiv_state.json")
    state = load_state(state_file)
    seen_ids = set(state.get("seen_ids", []))
    last_run_iso = state.get("last_run_iso")

    if last_run_iso:
        cutoff = dt.datetime.fromisoformat(last_run_iso)
    else:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=days_back)

    print(f"Query: {search_query}")
    print(f"Cutoff: {cutoff.isoformat()}")
    try:
        xml = arxiv_query(search_query, max_results=max_results)
    except Exception as e:
        print(f"ERROR: failed to query arXiv API: {e}", file=sys.stderr)
        return 2

    entries = parse_atom(xml)
    # Freshness filter
    entries = entries_newer_than(entries, cutoff)
    # Keyword filters
    entries = filter_entries(entries, required_all, any_keywords)
    # Dedup by id
    entries = dedupe_by_id(entries, seen_ids)

    if not entries:
        print("No new matching entries.")
        # Update last_run time at least
        state["last_run_iso"] = dt.datetime.utcnow().isoformat(timespec="seconds")
        save_state(state_file, state)
        return 0

    digest = format_digest(entries, include_abstract=show_abs, max_items=digest_max)
    subject = f"[arXiv] Federated learning digest — {len(entries)} new item(s)"

    # Output to stdout (so logs/cron mail capture it)
    print("\n" + digest + "\n")

    # Optional: email + slack
    try:
        send_email_smtp(subject, digest)
    except Exception as e:
        print(f"Email failed: {e}", file=sys.stderr)

    try:
        send_slack_webhook(digest)
    except Exception as e:
        print(f"Slack failed: {e}", file=sys.stderr)

    # Update state: mark seen ids and last_run
    seen_ids.update(e["id"] for e in entries)
    state["seen_ids"] = sorted(seen_ids)
    state["last_run_iso"] = dt.datetime.utcnow().isoformat(timespec="seconds")
    save_state(state_file, state)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
