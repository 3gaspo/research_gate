#!/usr/bin/env python3
# Standard-library only. Alerts on new arXiv papers for your keywords.

from urllib.error import HTTPError, URLError
import socket
import os, json, textwrap, ssl, smtplib, email.utils
import urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import time, random

ARXIV_API = "https://export.arxiv.org/api/query"

def getenv(name, default=None):
    v = os.environ.get(name, default)
    return v if v not in (None, "") else default

def csv(name, default=""):
    s = getenv(name, default)
    return [x.strip() for x in s.split(",") if x.strip()]

def rfc3339_to_naive_utc(s):
    """Parse arXiv timestamps like 2025-11-10T18:59:53Z into naive UTC datetimes."""
    if not s:
        return None
    try:
        # Modern ISO 8601 parsing (Python 3.11+)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        try:
            # Fallback for older email-style dates
            dt = email.utils.parsedate_to_datetime(s)
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return None


def build_query():
    # categories
    cats = csv("ARXIV_CATEGORIES", "cs.LG,cs.DC,stat.ML")
    cats_expr = " OR ".join(f"cat:{c}" for c in cats) if cats else ""

    # ANY keywords (default OR: federated learning / time series)
    any_kw = csv("ARXIV_ANY_KEYWORDS", "federated learning,time series")

    kw_terms = []
    for k in any_kw:
        kq = k.replace('"', '\\"')
        # search in title OR abstract
        kw_terms.append(f'ti:"{kq}"')
        kw_terms.append(f'abs:"{kq}"')
    kw_expr = "(" + " OR ".join(kw_terms) + ")" if kw_terms else ""

    # Optional manual override (advanced users)
    main = getenv("ARXIV_MAIN_QUERY")  # if set, you can still combine below

    # Combine
    parts = []
    if cats_expr: parts.append(f"({cats_expr})")
    if kw_expr:   parts.append(kw_expr)
    if main:      parts.append(f"({main})")

    return " AND ".join(parts) if parts else 'all:"*"'


def http_get(url, headers=None, timeout=45):
    """HTTP GET with polite retries/backoff; handles 429/5xx and timeouts."""
    hdrs = headers or {"User-Agent": "arxiv-topic-alert/1.0 (+github-actions)"}
    max_retries = 5
    base_backoff = 3  # seconds

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")

        except HTTPError as e:
            # Retry on rate limits / transient server errors
            if e.code in (429, 500, 502, 503, 504):
                retry_after = 0
                try:
                    retry_after = int(e.headers.get("Retry-After", "0") or 0)
                except Exception:
                    pass
                sleep_s = max(retry_after, base_backoff * (2 ** attempt) + random.uniform(0, 1))
                print(f"HTTP {e.code} from arXiv; retrying in {sleep_s:.1f}s ({attempt+1}/{max_retries})")
                time.sleep(sleep_s)
                continue
            # Non-retriable
            raise

        except (URLError, TimeoutError, socket.timeout) as e:
            sleep_s = base_backoff * (2 ** attempt) + random.uniform(0, 1)
            print(f"Network timeout/error '{e}'; retrying in {sleep_s:.1f}s ({attempt+1}/{max_retries})")
            time.sleep(sleep_s)
            continue

    print("ERROR: arXiv fetch failed after retries; continuing with empty feed.")
    return "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"


def arxiv_fetch(search_query, max_results=200):
    params = dict(
        search_query=search_query,
        start=0,
        max_results=max_results,
        sortBy="lastUpdatedDate",   # was "submittedDate"
        sortOrder="descending",
    )
    url = ARXIV_API + "?" + urllib.parse.urlencode(params)
    return http_get(url)


def parse_entries(atom_xml):
    ns = {"a":"http://www.w3.org/2005/Atom"}
    root = ET.fromstring(atom_xml)
    out = []
    for e in root.findall("a:entry", ns):
        ent = {
            "id": (e.findtext("a:id", "", ns) or "").strip(),
            "title": (e.findtext("a:title", "", ns) or "").strip().replace("\n"," "),
            "summary": (e.findtext("a:summary", "", ns) or "").strip().replace("\n"," "),
            "updated": (e.findtext("a:updated","",ns) or "").strip(),
            "published": (e.findtext("a:published","",ns) or "").strip(),
            "authors": ", ".join([(a.findtext("a:name","",ns) or "").strip()
                                  for a in e.findall("a:author", ns) if (a.findtext("a:name","",ns) or "").strip()]),
            "link": ""
        }
        for l in e.findall("a:link", ns):
            if l.attrib.get("rel") == "alternate":
                ent["link"] = l.attrib.get("href","")
                break
        out.append(ent)
    return out

def load_state(path): 
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_state(path, obj):
    with open(path,"w",encoding="utf-8") as f: json.dump(obj,f,indent=2,ensure_ascii=False)

def filter_by_time(entries, cutoff):
    out=[]
    for e in entries:
        t = max([x for x in [rfc3339_to_naive_utc(e["published"]), rfc3339_to_naive_utc(e["updated"])] if x], default=None)
        if t and t >= cutoff: out.append(e)
    return out

def filter_by_keywords(entries, required_all=None, any_keywords=None):
    R = [s.lower() for s in required_all]
    A = [s.lower() for s in any_keywords]
    out=[]
    for e in entries:
        hay = (e["title"]+" "+e["summary"]).lower()
        if R and not all(tok in hay for tok in R): continue
        if A and not any(tok in hay for tok in A): continue
        out.append(e)
    return out

def dedupe(entries, seen):
    return [e for e in entries if e["id"] not in seen]

def digest(
    entries,
    include_abs=True,
    max_items=50,
    query=None,
    cats=None,
    required_all=None,
    any_keywords=None,
    cutoff=None,
    counts=None,              # NEW: dict with step-by-step counts
    delivery=None             # NEW: dict like {"email":"sent/skipped", "slack":"sent/skipped"}
):
    """Format a human-readable digest and include ALL search settings + diagnostics."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"arXiv topic digest — {now}", ""]

    # --- ALWAYS print context ---
    lines.append(f"Query: {query or '(none)'}")
    lines.append(f"Categories: {', '.join(cats) if cats else '(none)'}")
    lines.append(f"Required ALL: {', '.join(required_all) if required_all else '(none)'}")
    lines.append(f"ANY keywords: {', '.join(any_keywords) if any_keywords else '(none)'}")
    lines.append(f"Cutoff (UTC): {cutoff.isoformat(timespec='seconds') if cutoff else '(none)'}")

    # --- ALWAYS print diagnostics ---
    if counts:
        lines.append(
            "Counts → "
            f"fetched:{counts.get('fetched', 0)} | "
            f"after_time:{counts.get('after_time', 0)} | "
            f"after_keywords:{counts.get('after_kw', 0)} | "
            f"after_dedupe:{counts.get('after_dedupe', 0)} | "
            f"seen_ids:{counts.get('seen', 0)}"
        )
    else:
        lines.append("Counts → fetched:0 | after_time:0 | after_keywords:0 | after_dedupe:0 | seen_ids:0")

    # --- ALWAYS print delivery status ---
    if delivery:
        lines.append(
            f"Delivery → email:{delivery.get('email','skipped')} | slack:{delivery.get('slack','skipped')}"
        )
    else:
        lines.append("Delivery → email:skipped | slack:skipped")

    lines.append("")  # blank line before entries

    # --- Entries (or an explicit message) ---
    if not entries:
        lines.append("No new matching entries.")
        return "\n".join(lines)

    for i, e in enumerate(entries[:max_items], 1):
        lines.append(f"{i}. {e['title']} ({e['published'][:10]})")
        if e["authors"]:
            lines.append(f"   {e['authors']}")
        lines.append(f"   {e['link'] or e['id']}")
        if include_abs and e["summary"]:
            lines.append("   Abstract: " + textwrap.shorten(e["summary"], width=600, placeholder=" …"))
        lines.append("")

    if len(entries) > max_items:
        lines.append(f"(+{len(entries) - max_items} more)")
    return "\n".join(lines).strip()

def send_email(subject, body):
    host=getenv("SMTP_HOST"); port=int(getenv("SMTP_PORT","587"))
    user=getenv("SMTP_USER"); pwd=getenv("SMTP_PASS")
    from_addr=getenv("SMTP_FROM", user or ""); to_addrs=csv("SMTP_TO")
    if not (host and from_addr and to_addrs):
        print("Email not sent: SMTP not fully configured."); return
    msg = f"From: {from_addr}\r\nTo: {', '.join(to_addrs)}\r\nSubject: {subject}\r\n\r\n{body}"
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        if user and pwd: s.login(user, pwd)
        s.sendmail(from_addr, to_addrs, msg.encode("utf-8"))
    print("Email sent.")

def send_slack(text):
    url=getenv("SLACK_WEBHOOK_URL"); 
    if not url: return
    data=json.dumps({"text":text}).encode("utf-8")
    req=urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print("Posted to Slack.")
    except Exception as e:
        print(f"Slack failed: {e}")

def main():
    state_file = getenv("STATE_FILE", "arxiv_state.json")
    state = load_state(state_file)
    seen = set(state.get("seen_ids", []))
    last = state.get("last_run_iso")
    cutoff = datetime.fromisoformat(last) if last else (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(getenv("ARXIV_DAYS_BACK","7"))))

    required_all = csv("ARXIV_REQUIRED_KEYWORDS", "")
    any_keywords = csv("ARXIV_ANY_KEYWORDS", "")

    query = build_query()
    max_results = int(getenv("ARXIV_MAX_RESULTS","200"))

    time.sleep(random.uniform(3, 9))
    xml = arxiv_fetch(query, max_results=max_results)
    entries_raw = parse_entries(xml)
    after_time = filter_by_time(entries_raw, cutoff)
    after_kw = filter_by_keywords(after_time, required_all, any_keywords)
    entries = dedupe(after_kw, seen)
    
    counts = {
        "fetched": len(entries_raw),
        "after_time": len(after_time),
        "after_kw": len(after_kw),
        "after_dedupe": len(entries),
        "seen": len(seen),
    }

    # --- Decide delivery status WITHOUT sending yet (so the digest reflects reality) ---
    # Email config check
    smtp_host = getenv("SMTP_HOST")
    smtp_from = getenv("SMTP_FROM", getenv("SMTP_USER") or "")
    smtp_to   = csv("SMTP_TO")
    smtp_configured = bool(smtp_host and smtp_from and smtp_to)

    # Slack config check
    slack_configured = bool(getenv("SLACK_WEBHOOK_URL"))

    if entries:
        email_status = "will send" if smtp_configured else "skipped (SMTP not configured)"
        slack_status = "will send" if slack_configured else "skipped (no webhook)"
    else:
        email_status = "skipped (0 entries)"
        slack_status = "skipped (0 entries)"

    delivery = {"email": email_status, "slack": slack_status}

    # --- Build digest ALWAYS (header + counts + delivery + entries/none) ---
    body = digest(
        entries,
        include_abs=(getenv("SHOW_ABSTRACT", "1") == "1"),
        max_items=int(getenv("DIGEST_MAX_ITEMS", "50")),
        query=query,
        cats=csv("ARXIV_CATEGORIES", "cs.LG,cs.DC,stat.ML"),
        required_all=required_all,
        any_keywords=any_keywords,
        cutoff=cutoff,
        counts=counts,
        delivery=delivery,
    )

    # Print digest to logs ALWAYS
    print("\n" + body + "\n")

    # --- Actually send only if there are entries and the destination is configured ---
    if entries and smtp_configured:
        try:
            send_email(f"[arXiv] {len(entries)} new item(s)", body)
        except Exception as e:
            print(f"Email error: {e}")
    if entries and slack_configured:
        try:
            send_slack(body)
        except Exception as e:
            print(f"Slack error: {e}")

    # --- Always update state and exit normally ---
    seen.update(e["id"] for e in entries)
    state["seen_ids"] = sorted(seen)
    state["last_run_iso"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    save_state(state_file, state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
