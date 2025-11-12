#!/usr/bin/env python3
import os, sys, time, textwrap, random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

try:
    import requests  # pip install requests
except ImportError:
    print("Missing dependency 'requests'. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


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
    parts = []
    for kw in keywords:
        parts.append(f'"{kw}"' if " " in kw else kw)
    if fields_of_study:
        parts += fields_of_study
    return " OR ".join(parts) if parts else "machine learning"


def _request_with_backoff(url: str, headers: dict, params: dict, max_retries: int, base_sleep: float):
    """GET with exponential backoff that respects Retry-After on 429."""
    attempt = 0
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        # 429: rate limited
        attempt += 1
        if attempt > max_retries:
            resp.raise_for_status()  # will raise HTTPError 429
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = int(retry_after)
        else:
            # exponential backoff with jitter
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        time.sleep(sleep_s)


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
    remaining = max_results
    current_page_size = max(10, min(page_size, 100))

    while remaining > 0:
        limit = min(current_page_size, remaining)
        params = {"query": query, "fields": fields, "limit": str(limit), "offset": str(offset)}

        try:
            r = _request_with_backoff(base, headers, params, max_retries=max_retries, base_sleep=base_sleep)
        except requests.HTTPError as e:
            # If we still get 429 after retries, halve the page size and try once more loop iteration
            if e.response is not None and e.response.status_code == 429 and current_page_size > 10:
                current_page_size = max(10, current_page_size // 2)
                continue
            raise

        data = r.json().get("data", [])
        if not data:
            break

        added = 0
        for p in data:
            pubdate = p.get("publicationDate")
            year = p.get("year")
            keep = False
            if pubdate:
                try:
                    dt = datetime.fromisoformat(pubdate.replace("Z", "+00:00"))
                    keep = dt >= since_dt
                except Exception:
                    keep = True
            elif year:
                keep = int(year) >= since_dt.year
            else:
                keep = True
            if not keep:
                continue

            title = (p.get("title") or "").strip()
            abstract = p.get("abstract") or ""
            title_ok = text_has_keywords(title, keywords, intersect)
            abstract_ok = text_has_keywords(abstract, keywords, intersect)
            if not (title_ok or abstract_ok):
                continue

            fos = p.get("fieldsOfStudy") or []
            if fields_of_study:
                if not any(fs.lower() in [f.lower() for f in fos] for fs in fields_of_study):
                    continue

            url = p.get("url") or ""
            ext = p.get("externalIds") or {}
            if not url and "ArXiv" in ext:
                url = f"https://arxiv.org/abs/{ext['ArXiv']}"

            snippet = ""
            if os.getenv("INCLUDE_ABSTRACTS", "false").lower() == "true" and abstract:
                s = " ".join(abstract.split())
                snippet = f"\n  – {textwrap.shorten(s, width=180, placeholder='…')}"

            results.append({
                "title": title,
                "url": url,
                "date": pubdate or (str(year) if year else "N/A"),
                "cat": ", ".join(fos) if fos else "N/A",
                "snippet": snippet,
            })
            added += 1
            if len(results) >= max_results:
                break

        if added == 0 and len(data) == 0:
            break

        offset += len(data)
        remaining = max_results - len(results)

        if delay:
            time.sleep(delay)

    return results


def main():
    default_keywords = ["federated learning", "time series"]
    default_fos = []  # e.g., ["Computer Science", "Mathematics"]

    keywords = normalize_kw_list(getenv_list("S2_KEYWORDS", default_keywords))
    intersect = os.getenv("S2_INTERSECT_KW", "false").lower() == "true"
    days = int(os.getenv("S2_DAYS", "3"))
    # Safer defaults to avoid 429s:
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
