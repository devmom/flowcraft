"""Web Scrape Skill Script — deterministic HTML extraction."""

import json
import re
import sys
from urllib.parse import urljoin, urlparse

def main():
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid input JSON: {e}"}))
        return

    url = params.get("url", "")
    extract = params.get("extract", ["title", "text", "links"])
    output_format = params.get("output_format", "json")

    if not url:
        print(json.dumps({"status": "error", "message": "url is required"}))
        return

    try:
        from urllib.request import urlopen, Request
        import gzip
        req = Request(url, headers={"User-Agent": "FlowCraft/1.0", "Accept-Encoding": "gzip, deflate"})
        with urlopen(req, timeout=15) as resp:
            raw_data = resp.read()
            # Handle gzip compression
            if resp.headers.get("Content-Encoding") == "gzip" or raw_data[:2] == b'\x1f\x8b':
                raw_data = gzip.decompress(raw_data)
            html = raw_data.decode("utf-8", errors="replace")
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to fetch URL: {e}"}))
        return

    result = {"status": "success", "url": url}

    # Extract title
    if "title" in extract:
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        result["title"] = m.group(1).strip() if m else "No title found"

    # Extract text (strip HTML tags)
    if "text" in extract:
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.I | re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        result["text"] = text[:5000]
        result["text_length"] = len(text)

    # Extract links
    if "links" in extract:
        links = re.findall(r"""<a[^>]+href=["'](.*?)["']""", html, re.I)
        base = urlparse(url)
        full_links = []
        for link in links[:50]:
            full = urljoin(url, link)
            full_links.append(full)
        result["links"] = full_links
        result["link_count"] = len(full_links)

    result["summary"] = f"Extracted from {url}: title={bool(result.get('title'))}, text={result.get('text_length', 0)} chars, {result.get('link_count', 0)} links"

    if output_format == "csv" and "links" in extract:
        lines = ["index,url"] + [f"{i},{link}" for i, link in enumerate(result["links"])]
        result["csv_output"] = "\n".join(lines)

    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
