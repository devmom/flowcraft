"""Advanced Web Scrape — handles SPA pages like Bilibili, YouTube."""

import json
import re
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def extract_meta(html: str, name: str) -> str:
    """Extract <meta name=... content=...> or <meta property=og:... content=...>"""
    for pattern in [
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
    ]:
        m = re.search(pattern, html, re.I)
        if m:
            return m.group(1).strip()
    return ""


def extract_jsonld(html: str) -> dict:
    """Extract JSON-LD structured data."""
    m = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return {}


def extract_bilibili(html: str) -> dict:
    """Bilibili-specific extraction from __INITIAL_STATE__."""
    result = {}
    # B站新页面结构
    m = re.search(
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*\(function',
        html, re.S,
    )
    if not m:
        # 老页面
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.S)
    if m:
        try:
            state = json.loads(m.group(1))
            vd = state.get("videoData", {})
            result["title"] = vd.get("title", "")
            result["description"] = vd.get("desc", "")
            result["author"] = state.get("upData", {}).get("name", "")
            result["views"] = vd.get("stat", {}).get("view", "")
            result["danmaku"] = vd.get("stat", {}).get("danmaku", "")
            result["likes"] = vd.get("stat", {}).get("like", "")
            result["coins"] = vd.get("stat", {}).get("coin", "")
            result["favorites"] = vd.get("stat", {}).get("favorite", "")
            result["tags"] = [t.get("tag_name", "") for t in vd.get("tag", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return result


def main():
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid input: {e}"}, ensure_ascii=False))
        return

    url = params.get("url", "")
    extract = params.get("extract", ["title", "description", "keywords", "author"])

    if not url:
        print(json.dumps({"status": "error", "message": "url is required"}, ensure_ascii=False))
        return

    # Fetch page
    try:
        import gzip as _gz
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urlopen(req, timeout=15) as resp:
            raw_data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw_data[:2] == b'\x1f\x8b':
                raw_data = _gz.decompress(raw_data)
            html = raw_data.decode("utf-8", errors="replace")
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"HTTP fetch failed: {e}"}, ensure_ascii=False))
        return

    result = {"status": "success", "url": url}

    # Check if it's Bilibili
    is_bilibili = "bilibili.com" in url

    if is_bilibili:
        bdata = extract_bilibili(html)
        if bdata:
            result.update(bdata)
            result["source"] = "bilibili_initial_state"

    # Fallback: meta tags
    if not result.get("title"):
        result["title"] = (
            extract_meta(html, "og:title")
            or extract_meta(html, "title")
            or extract_meta(html, "twitter:title")
            or ""
        )

    if "description" in extract and not result.get("description"):
        result["description"] = (
            extract_meta(html, "og:description")
            or extract_meta(html, "description")
            or extract_meta(html, "twitter:description")
            or ""
        )

    if "keywords" in extract:
        result["keywords"] = extract_meta(html, "keywords").split(",") if extract_meta(html, "keywords") else []

    if "author" in extract and not result.get("author"):
        result["author"] = extract_meta(html, "author") or extract_meta(html, "article:author") or ""

    # JSON-LD as last resort
    if not result.get("title"):
        jld = extract_jsonld(html)
        if jld:
            result["jsonld_title"] = jld.get("name", "") or jld.get("headline", "")
            result["jsonld_desc"] = jld.get("description", "")
            result["jsonld_author"] = (jld.get("author", {}) or {}).get("name", "")

    # Summary
    parts = []
    if result.get("title"):
        parts.append(f"title='{result['title'][:60]}'")
    if result.get("author"):
        parts.append(f"author='{result['author']}'")
    if result.get("views"):
        parts.append(f"views={result['views']}")
    result["summary"] = f"Extracted: {', '.join(parts)}" if parts else "Extraction successful"

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
