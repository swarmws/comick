#!/usr/bin/env python3
"""
Comick Scraper Microservice
A FastAPI service for scraping Comick lists
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as curl_requests
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
        
        resp = curl_requests.get(follows_url, impersonate="chrome120")
        
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
    from urllib.parse import quote
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://comick.dev/",
        "Origin": "https://comick.dev",
    }

    # 1. Search for the comic
    search_url = f"https://api.comick.dev/v1.0/search?q={quote(slug)}&limit=49&page=1&content_rating=safe&content_rating=suggestive&content_rating=erotica&content_rating=pornographic"
    search_resp = curl_requests.get(search_url, headers=headers, impersonate="chrome120", timeout=15)
    print(f"Search URL: {search_resp.url}")
    print(f"Search status: {search_resp.status_code}")
    print(f"Search response: {search_resp.text[:500]}")

    if search_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Not Found")

    items = search_resp.json()
    if not isinstance(items, list):
        items = []

    first = next((item for item in items if isinstance(item, dict) and item.get("slug")), None)
    if not first:
        raise HTTPException(status_code=400, detail="Not Found")

    comic_slug = first["slug"]

    # 2. Get build ID from homepage
    home_resp = curl_requests.get("https://comick.dev", impersonate="chrome120", timeout=15)
    print(f"Homepage status: {home_resp.status_code}")
    build_id = None
    if home_resp.status_code == 200:
        match = re.search(r'<script id="__NEXT_DATA__".*?>(.*?)</script>', home_resp.text, re.DOTALL)
        if match:
            try:
                build_id = json.loads(match.group(1)).get("buildId")
                print(f"Build ID: {build_id}")
            except Exception as e:
                print(f"Build ID parse error: {e}")

    if not build_id:
        raise HTTPException(status_code=400, detail="Not Found")

    # 3. Fetch covers from Next.js data
    next_resp = curl_requests.get(
        f"https://comick.dev/_next/data/{build_id}/comic/{comic_slug}/cover.json",
        params={"slug": comic_slug},
        headers={**headers, "Referer": f"https://comick.dev/comic/{comic_slug}"},
        impersonate="chrome120",
        timeout=15
    )
    print(f"Next.js covers status: {next_resp.status_code}")
    print(f"Next.js covers response: {next_resp.text[:300]}")
    if next_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Not Found")

    page_props = next_resp.json().get("pageProps", {})
    raw = page_props.get("md_covers") or page_props.get("comic", {}).get("md_covers", [])
    covers = [
        f"https://meo.comick.pictures/{c['b2key']}"
        for c in raw
        if isinstance(c, dict) and c.get("b2key")
    ]

    if not covers:
        raise HTTPException(status_code=400, detail="Not Found")
    return covers


@app.get("/comments")
async def comments_endpoint(url: str):
    user_id_match = re.search(r"/user/([a-f0-9-]{36})", url)
    if not user_id_match:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    user_id = user_id_match.group(1)
    all_comments = []

    for page in range(1, 21):
        try:
            resp = curl_requests.get(
                f"https://api.comick.dev/user/{user_id}/comments?page={page}",
                impersonate="chrome120",
                timeout=15
            )
            if resp.status_code != 200:
                break

            data = resp.json()
            if not data:
                break

            for item in data:
                if item is None:
                    continue

                file_name = item.get("file")
                if file_name:
                    image_url = f"https://meo.comick.pictures/{file_name}"
                    has_image = True
                else:
                    image_url = None
                    has_image = False

                all_comments.append({
                    "content": item.get("parsed"),
                    "image": has_image,
                    "image_url": image_url,
                    "date": item.get("created_at"),
                    "manga_title": item.get("md_comics", {}).get("title")
                })

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return all_comments

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4775,
        reload=False,
        log_level="info"
    )
