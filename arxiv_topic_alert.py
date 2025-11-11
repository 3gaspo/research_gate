#!/usr/bin/env python3
# Standard-library only. Alerts on new arXiv papers for your keywords.

import os, json, textwrap, ssl, smtplib, email.utils
import urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

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
    cats = csv("ARXIV_CATEGORIES", "cs.LG,cs.DC,stat.ML")
    cats_expr = " OR ".join(f"cat:{c}" for c in cats) if cats else ""
    main = getenv("ARXIV_MAIN_QUERY")  # optional exact arXiv query
    if cats_expr and main: return f"({cats_expr}) AND {main}"
    return main or cats_expr or 'all:"*"'  # fallback: everything (not recommended)

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent":"arxiv-topic-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def arxiv_fetch(search_query, max_results=200):
    params = dict(search_query=search_query, start=0, max_results=max_results,
                  sortBy="submittedDate", sortOrder="descending")
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

def digest(entries, include_abs=True, max_items=50):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines=[f"arXiv topic digest — {now}\n"]
    for i,e in enumerate(entries[:max_items],1):
        lines.append(f"{i}. {e['title']} ({e['published'][:10]})")
        if e["authors"]: lines.append(f"   {e['authors']}")
        lines.append(f"   {e['link'] or e['id']}")
        if include_abs and e["summary"]:
            lines.append("   Abstract: "+textwrap.shorten(e["summary"], width=600, placeholder=" …"))
        lines.append("")
    if len(entries)>max_items: lines.append(f"(+{len(entries)-max_items} more)")
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
    cutoff = datetime.fromisoformat(last) if last else (datetime.utcnow() - timedelta(days=int(getenv("ARXIV_DAYS_BACK","7"))))

    # Defaults: required keywords = ["federated learning","time series"]
    required_all = csv("ARXIV_REQUIRED_KEYWORDS"), "")
    any_keywords = csv("ARXIV_ANY_KEYWORDS", "")

    query = build_query()
    max_results = int(getenv("ARXIV_MAX_RESULTS","200"))
    print(f"Query: {query}")
    xml = arxiv_fetch(query, max_results=max_results)
    entries = parse_entries(xml)
    entries = filter_by_time(entries, cutoff)
    entries = filter_by_keywords(entries, required_all, any_keywords)
    entries = dedupe(entries, seen)

    if not entries:
        print("No new matching entries.")
        state["last_run_iso"] = datetime.utcnow().isoformat(timespec="seconds")
        save_state(state_file, state)
        return 0

    body = digest(entries, include_abs=(getenv("SHOW_ABSTRACT","1")=="1"),
                  max_items=int(getenv("DIGEST_MAX_ITEMS","50")))
    print("\n"+body+"\n")
    try: send_email(f"[arXiv] {len(entries)} new item(s)", body)
    except Exception as e: print(f"Email error: {e}")
    try: send_slack(body)
    except Exception as e: print(f"Slack error: {e}")

    seen.update(e["id"] for e in entries)
    state["seen_ids"] = sorted(seen)
    state["last_run_iso"] = datetime.utcnow().isoformat(timespec="seconds")
    save_state(state_file, state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
