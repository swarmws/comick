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

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4775,
        reload=False,
        log_level="info"
    )


