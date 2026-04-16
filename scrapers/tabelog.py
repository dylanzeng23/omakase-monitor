import asyncio
import logging
import re

import httpx

from models import Restaurant

logger = logging.getLogger(__name__)

TABELOG_BASE = "https://tabelog.com/en/tokyo/rstLst/"


async def scrape_tabelog_top_rated(min_rating: float = 4.3) -> list[Restaurant]:
    """Scrape Tabelog Tokyo restaurants sorted by rating, stopping when rating < min_rating."""
    restaurants = []
    start = 1
    page_size = 20

    # Use Japanese Tabelog (better HTML structure) and English Tabelog for names
    # Note: Tabelog pagination is broken via plain HTTP — only page 1 works
    # But page 1 sorted by rating gives us top 20, which covers 4.50-4.66
    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        follow_redirects=True,
    ) as client:
        # Fetch English page for English names
        en_url = f"{TABELOG_BASE}?SrtT=rt&Srt=D"
        logger.info(f"Scraping Tabelog English: {en_url}")
        en_resp = await client.get(en_url)

        # Fetch Japanese page for Japanese names (used for fuzzy matching with omakase.in)
        jp_url = "https://tabelog.com/tokyo/rstLst/?SrtT=rt&Srt=D"
        logger.info(f"Scraping Tabelog Japanese: {jp_url}")
        jp_resp = await client.get(jp_url)

        # Parse English page
        en_html = en_resp.text
        en_ratings = re.findall(r'>(\d\.\d{1,2})<', en_html)
        en_ratings = [r for r in en_ratings if 3.0 <= float(r) <= 5.0]
        en_names = re.findall(r'list-rst__rst-name[^>]*>([^<]+)<', en_html)
        if not en_names:
            en_names = re.findall(r'>(\d\.\d{1,2})<.*?<a[^>]*>([^<]+)</a>', en_html)

        # Parse Japanese page
        jp_html = jp_resp.text
        jp_names = re.findall(r'list-rst__rst-name-target[^>]*>([^<]+)<', jp_html)

        min_entries = min(len(en_names), len(en_ratings))
        for i in range(min_entries):
            rating = float(en_ratings[i])
            if rating < min_rating:
                logger.info(f"Reached rating threshold ({min_rating}). Stopping at #{i+1}.")
                break

            en_name = en_names[i].strip()
            jp_name = jp_names[i].strip() if i < len(jp_names) else ""

            restaurants.append(Restaurant(
                name=en_name,
                omakase_code="",
                tabelog_rating=rating,
                cuisine="",
                location="Tokyo",
                notes=jp_name,  # store JP name for fuzzy matching
            ))
            logger.info(f"  {rating} - {en_name} ({jp_name})")

    logger.info(f"Scraped {len(restaurants)} restaurants from Tabelog (rating >= {min_rating})")
    return restaurants
