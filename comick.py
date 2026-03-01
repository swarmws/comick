#!/usr/bin/env python3
"""
Comick Scraper Microservice
A FastAPI service for scraping Comick lists
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import cloudscraper
import json
import re
from typing import Dict, List, Any
import uvicorn

app = FastAPI(
    title="Comick Scraper API",
    description="Microservice for scraping Comick manga lists",
    version="1.0.0"
)

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def scrape_comick_list(url: str) -> Dict[str, Any]:
    """Scrape a Comick user's manga list"""
    try:
        # Extract user ID from URL
        user_id_match = re.search(r"/user/([a-f0-9-]{36})", url)
        if not user_id_match:
            return {"error": "Invalid user ID format"}
        
        user_id = user_id_match.group(1)
        follows_url = f"https://api.comick.dev/user/{user_id}/follows"
        
        # Use cloudscraper to bypass Cloudflare protection
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(follows_url)
        
        if resp.status_code != 200:
            return {"error": f"Failed to fetch data: {resp.status_code}"}
        
        data = resp.json()
        
        # Extract titles from the response
        titles = []
        if isinstance(data, list):
            for item in data:
                if "md_comics" in item and "title" in item["md_comics"]:
                    titles.append({
                        "title": item["md_comics"]["title"],
                        "comick_id": item["md_comics"].get("slug", item["md_comics"].get("id", "unknown"))
                    })
        
        return {"titles": titles}
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Comick Scraper API is running", "status": "healthy"}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "comick-scraper"}

@app.post("/scrape")
async def scrape_endpoint(request: Dict[str, str]):
    """Scrape a Comick user's manga list"""
    url = request.get("url")
    
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    
    # Validate Comick URL format
    comick_url_pattern = r'^https?://comick\.(io|dev)/user/[^/]+(/list)?$'
    if not re.match(comick_url_pattern, url):
        raise HTTPException(
            status_code=400, 
            detail="Invalid Comick URL format. Expected: https://comick.dev/user/[user-id] or https://comick.dev/user/[user-id]/list"
        )
    
    result = scrape_comick_list(url)
    
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    
    return result

@app.get("/scrape")
async def scrape_get(url: str):
    """Scrape a Comick user's manga list (GET endpoint for convenience)"""
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    
    # Validate Comick URL format
    comick_url_pattern = r'^https?://comick\.(io|dev)/user/[^/]+(/list)?$'
    if not re.match(comick_url_pattern, url):
        raise HTTPException(
            status_code=400, 
            detail="Invalid Comick URL format. Expected: https://comick.dev/user/[user-id] or https://comick.dev/user/[user-id]/list"
        )
    
    result = scrape_comick_list(url)
    
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    
    return result

@app.get("/cover")
async def cover_endpoint(slug: str):
    """Fetch all cover image URLs for a comic by title search"""
    scraper = cloudscraper.create_scraper()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://comick.io/",
        "Origin": "https://comick.io",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 1. Resolve slug (accept direct slug; fallback to title search)
    def normalize_slug(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        return re.sub(r"-+", "-", value).strip("-")

    candidate_slugs = []
    input_slug = normalize_slug(slug)
    if input_slug:
        candidate_slugs.append(input_slug)

    try:
        search_resp = scraper.get(
            "https://api.comick.dev/v1.0/search/",
            params={"q": slug, "limit": 10, "type": "comic"},
            headers=headers,
            timeout=15
        )
        if search_resp.status_code == 200:
            items = search_resp.json()
            if not isinstance(items, list):
                items = items.get("result", items.get("results", items.get("data", [])))
            if isinstance(items, list):
                for item in items:
                    candidate = item.get("slug") if isinstance(item, dict) else None
                    if candidate and candidate not in candidate_slugs:
                        candidate_slugs.append(candidate)
    except Exception:
        pass

    if not candidate_slugs:
        raise HTTPException(status_code=400, detail="Not Found")

    # 2. Fetch covers from main API first; keep first matching slug with covers
    comic_slug = None
    covers = []
    for candidate in candidate_slugs:
        try:
            comic_resp = scraper.get(
                f"https://api.comick.dev/comic/{candidate}",
                headers=headers,
                timeout=15
            )
            if comic_resp.status_code != 200:
                continue
            comic_data = comic_resp.json()
            raw = comic_data.get("comic", {}).get("md_covers", [])
            candidate_covers = [
                f"https://meo.comick.pictures/{c['b2key']}"
                for c in raw
                if isinstance(c, dict) and c.get("b2key")
            ]
            if candidate_covers:
                comic_slug = candidate
                covers = candidate_covers
                break
        except Exception:
            continue

    # If no covers from main API, still try Next.js using the first candidate
    if comic_slug is None:
        comic_slug = candidate_slugs[0]

    comic_headers = {**headers, "Referer": f"https://comick.io/comic/{comic_slug}"}

    # 3. Get build ID from homepage
    build_id = None
    home_resp = scraper.get("https://comick.dev", headers={"User-Agent": headers["User-Agent"]}, timeout=15)
    if home_resp.status_code == 200:
        match = re.search(r'<script id="__NEXT_DATA__".*?>(.*?)</script>', home_resp.text, re.DOTALL)
        if match:
            try:
                build_id = json.loads(match.group(1)).get("buildId")
            except Exception:
                pass

    if not build_id:
        raise HTTPException(status_code=400, detail="Not Found")

    # 4. Fetch additional covers from Next.js data
    try:
        next_resp = scraper.get(
            f"https://comick.io/_next/data/{build_id}/comic/{comic_slug}/cover.json",
            params={"slug": comic_slug},
            headers=comic_headers,
            timeout=15
        )
        if next_resp.status_code == 200:
            page_props = next_resp.json().get("pageProps", {})
            raw = page_props.get("md_covers") or page_props.get("comic", {}).get("md_covers", [])
            extra_covers = [
                f"https://meo.comick.pictures/{c['b2key']}"
                for c in raw
                if isinstance(c, dict) and c.get("b2key")
            ]
            for cover in extra_covers:
                if cover not in covers:
                    covers.append(cover)
    except Exception:
        pass

    if not covers:
        raise HTTPException(status_code=400, detail="Not Found")
    return covers


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4775,
        reload=False,
        log_level="info"
    )
