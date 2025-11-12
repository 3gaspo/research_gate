#!/usr/bin/env python3
# semanticscholar_digest.py
import os, sys, time, textwrap, random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

try:
    import requests  # pip install requests
except ImportError:
    print("Missing dependency 'requests'. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


# ---------------------------- helpers ----------------------------

def getenv_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()] if raw.strip() else default

def normalize_kw_list(keywords: List[str]) -> List[str]:
    return [k.strip().lower() for k in keywords if k.strip()]

def text_has_keywords(text: str, keywords: List[str], intersect: bool) -> bool:
    t = (text or "").lower()
    if not keywords:
        return True
    return all(kw in t for kw in keywords) if intersect else any(kw in t for kw in keywords)

def build_free_text_query(keywords: List[str], fields_of_study: List[str]) -> str:
    # Quote multi-word phrases; join with OR for recall. FOS tokens nudge relevance.
    parts = [(f'"{kw}"' if " " in kw else kw) for kw in keywords]
    if fields_of_study:
        parts += fields_of_study
    return " OR ".join(parts) if parts else "machine learning"

def _parse_pubdate_utc(s: str) -> Optional[datetime]:
    # Accept "YYYY-MM-DD" or ISO with/without 'Z'
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _request_with_backoff(url: str, headers: dict, params: dict, max_retries: int, base_sleep: float):
    """GET with exponential backoff, honoring Retry-After on 429."""
    attempt = 0
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        attempt += 1
        if attempt > max_retries:
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = int(retry_after)
        else:
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        time.sleep(sleep_s)


# ---------------------------- core search ----------------------------

def fetch_semantic_scholar(
    keywords: List[str],
    since_dt: datetime,
    max_results: int,
    page_size: int,
    delay: float,
    api_key: Optional[str],
    intersect: bool,
    fields_of_study: List[str],
    max_retries: int = 4,
    base_sleep: float = 1.5,
):
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    query = build_free_text_query(keywords, fields_of_study)
    fields = "title,abstract,year,publicationDate,url,externalIds,fieldsOfStudy"

    results = []
    offset = 0
    current_page_size = max(10, min(page_size, 100))

    # Guards to avoid deep offsets that can trigger 400s on some queries
    hard_max_offset = int(os.getenv("S2_MAX_OFFSET", "400"))
    # If we keep seeing old results, bail early
    stale_rows_seen = 0
    stale_rows_threshold = max(100, current_page_size * 3)

    while len(results) < max_results:
        if offset >= hard_max_offset:
            # Stop before hitting problematic offsets
            break

        limit = min(current_page_size, max_results - len(results))
        params = {"query": query, "fields": fields, "limit": str(limit), "offset": str(offset)}

        try:
            r = _request_with_backoff(base, headers, params, max_retries=max_retries, base_sleep=base_sleep)
        except requests.HTTPError as e:
            # If we hit 400 or 429 repeatedly, reduce page size and try next page; otherwise stop
            status = e.response.status_code if e.response is not None else None
            if status in (400, 429) and current_page_size > 10:
                current_page_size = max(10, current_page_size // 2)
                # skip ahead conservatively
                offset += current_page_size
                continue
            # give up gracefully
            break

        payload = r.json()
        data = payload.get("data", [])
        total = payload.get("total")  # may be absent

        if not data:
            break

        added_this_page = 0
        for p in data:
            pubdate = p.get("publicationDate")
            year = p.get("year")

            keep = False
            if pubdate:
                dt = _parse_pubdate_utc(pubdate)
                keep = (dt is not None) and (dt.date() >= since_dt.date())
            elif year:
                try:
                    keep = int(year) >= since_dt.year
                except Exception:
                    keep = False
            else:
                keep = False

            if not keep:
                stale_rows_seen += 1
                continue

            title = (p.get("title") or "").strip()
            abstract = p.get("abstract") or ""
            if not (text_has_keywords(title, keywords, intersect) or
                    text_has_keywords(abstract, keywords, intersect)):
                continue

            fos = p.get("fieldsOfStudy") or []
            if fields_of_study:
                lf = [f.lower() for f in fos]
                if not any(fs.lower() in lf for fs in fields_of_study):
                    continue

            url = p.get("url") or ""
            ext = p.get("externalIds") or {}
            if not url and "ArXiv" in ext:
                url = f"https://arxiv.org/abs/{ext['ArXiv']}"

            date_str = pubdate.split("T")[0] if pubdate else (str(year) if year else "N/A")

            snippet = ""
            if os.getenv("INCLUDE_ABSTRACTS", "false").lower() == "true" and abstract:
                s = " ".join(abstract.split())
                snippet = f"\n  – {textwrap.shorten(s, width=180, placeholder='…')}"

            results.append({
                "title": title,
                "url": url,
                "date": date_str,
                "cat": ", ".join(fos) if fos else "N/A",
                "snippet": snippet,
            })
            added_this_page += 1
            if len(results) >= max_results:
                break

        # Pagination bookkeeping
        offset += len(data)

        # Stop early if API says we've reached the end
        if total is not None and offset >= int(total):
            break

        # If we’re only seeing stale/too-old rows for a while, stop digging
        if stale_rows_seen >= stale_rows_threshold and len(results) == 0:
            break

        if delay:
            time.sleep(delay)

    return results


# ---------------------------- CLI ----------------------------

def main():
    default_keywords = ["federated learning", "time series"]
    default_fos = []  # e.g., ["Computer Science", "Mathematics"]

    keywords = normalize_kw_list(getenv_list("S2_KEYWORDS", default_keywords))
    intersect = os.getenv("S2_INTERSECT_KW", "false").lower() == "true"
    days = int(os.getenv("S2_DAYS", "3"))

    # Safer defaults to avoid 429/400
    max_results = int(os.getenv("S2_MAX_RESULTS", "100"))
    page_size = int(os.getenv("S2_PAGE_SIZE", "50"))
    delay = float(os.getenv("S2_DELAY", "1.5"))

    fields_of_study = getenv_list("S2_FIELDS", default_fos)
    include_abstracts = os.getenv("INCLUDE_ABSTRACTS", "false").lower() == "true"
    api_key = os.getenv("S2_API_KEY", "").strip() or None

    since = datetime.now(timezone.utc) - timedelta(days=days)

    matches = fetch_semantic_scholar(
        keywords=keywords,
        since_dt=since,
        max_results=max_results,
        page_size=page_size,
        delay=delay,
        api_key=api_key,
        intersect=intersect,
        fields_of_study=fields_of_study,
    )

    header = [
        f"Semantic Scholar digest – last {days} day(s)",
        ("Keywords: " + ", ".join(keywords)) if keywords else "Keywords: (none)",
        ("Fields of study: " + ", ".join(fields_of_study)) if fields_of_study else "Fields of study: (any)",
        ("Abstract snippets: on" if include_abstracts else "Abstract snippets: off"),
        "",
    ]

    if not matches:
        print("\n".join(header + [f"No matching papers found these last {days} days."]))
        return

    lines = header + [f"Found {len(matches)} paper(s):", ""]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{i}. {m['title']}  [{m['cat']}]  ({m['date']})")
        lines.append(f"   {m['url']}{m['snippet']}")
        lines.append("")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
