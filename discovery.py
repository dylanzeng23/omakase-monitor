#!/usr/bin/env python3
"""Phase 1: Discovery — scrape Tabelog + omakase.in catalog, fuzzy match, generate watchlist."""

import asyncio
import logging
import re
from pathlib import Path

import yaml
from thefuzz import fuzz

from models import Config, Restaurant
from scrapers.tabelog import scrape_tabelog_top_rated
from scrapers.omakase import OmakaseScraper

logger = logging.getLogger(__name__)

WATCHLIST_PATH = Path(__file__).parent / "watchlist.yaml"


def normalize_name(name: str) -> str:
    """Normalize restaurant name for comparison."""
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in ("(takeaway)", "takeaway", "(delivery)", "delivery"):
        name = name.replace(suffix, "")
    # Remove punctuation
    name = re.sub(r"[^\w\s]", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def fuzzy_match(tabelog_restaurants: list[Restaurant], omakase_restaurants: list[Restaurant],
                threshold: int = 75) -> list[dict]:
    """Fuzzy match Tabelog restaurants to omakase.in restaurants."""
    matches = []
    omakase_tokyo = [r for r in omakase_restaurants if "tokyo" in r.location.lower() or not r.location]

    for tabelog_r in tabelog_restaurants:
        t_name_en = normalize_name(tabelog_r.name)
        t_name_jp = tabelog_r.notes  # Japanese name stored in notes
        best_score = 0
        best_match = None

        for omakase_r in omakase_tokyo:
            o_name = normalize_name(omakase_r.name)

            # Try English name matching
            score_en = max(
                fuzz.token_sort_ratio(t_name_en, o_name),
                fuzz.partial_ratio(t_name_en, o_name),
                fuzz.token_set_ratio(t_name_en, o_name),
            )

            # Also try Japanese name if available
            score_jp = 0
            if t_name_jp:
                score_jp = max(
                    fuzz.token_sort_ratio(t_name_jp, omakase_r.name),
                    fuzz.partial_ratio(t_name_jp, omakase_r.name),
                )

            score = max(score_en, score_jp)

            if score > best_score:
                best_score = score
                best_match = omakase_r

        matches.append({
            "tabelog": tabelog_r,
            "omakase": best_match if best_score >= threshold else None,
            "score": best_score,
        })

    return matches


def generate_watchlist(matches: list[dict]) -> dict:
    """Generate watchlist.yaml content from fuzzy matches."""
    restaurants = []

    for m in matches:
        tabelog_r = m["tabelog"]
        omakase_r = m["omakase"]

        entry = {
            "name": tabelog_r.name,
            "tabelog_rating": tabelog_r.tabelog_rating,
            "tabelog_url": tabelog_r.tabelog_url,
            "cuisine": tabelog_r.cuisine or (omakase_r.cuisine if omakase_r else ""),
        }

        if omakase_r:
            entry["omakase_code"] = omakase_r.omakase_code
            entry["omakase_name"] = omakase_r.name
            entry["match_score"] = m["score"]
        else:
            entry["omakase_code"] = None
            entry["omakase_name"] = None
            entry["match_score"] = m["score"]
            entry["notes"] = "No match on omakase.in — verify manually"

        restaurants.append(entry)

    # Sort by Tabelog rating descending
    restaurants.sort(key=lambda r: r.get("tabelog_rating", 0), reverse=True)

    return {"restaurants": restaurants}


async def run_discovery(config: Config):
    """Run full discovery pipeline."""
    logger.info("=== Phase 1: Discovery ===")

    # Step 1: Scrape Tabelog
    logger.info("Step 1: Scraping Tabelog top-rated Tokyo restaurants...")
    tabelog_restaurants = await scrape_tabelog_top_rated(min_rating=config.min_tabelog_rating)
    logger.info(f"Found {len(tabelog_restaurants)} Tabelog restaurants (rating >= {config.min_tabelog_rating})")

    # Step 2: Scrape omakase.in catalog
    logger.info("Step 2: Scraping omakase.in restaurant catalog...")
    scraper = OmakaseScraper(config)
    omakase_restaurants = await scraper.scrape_catalog()
    logger.info(f"Found {len(omakase_restaurants)} omakase.in restaurants")

    # Step 3: Fuzzy match
    logger.info("Step 3: Cross-referencing restaurants...")
    matches = fuzzy_match(tabelog_restaurants, omakase_restaurants)

    matched_count = sum(1 for m in matches if m["omakase"])
    logger.info(f"Matched {matched_count}/{len(matches)} restaurants")

    # Step 4: Generate watchlist
    watchlist_data = generate_watchlist(matches)

    with open(WATCHLIST_PATH, "w") as f:
        yaml.dump(watchlist_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logger.info(f"Watchlist saved to {WATCHLIST_PATH}")

    # Print summary
    print(f"\n{'=' * 50}")
    print(f"Discovery Results")
    print(f"{'=' * 50}")
    print(f"Tabelog restaurants (>= {config.min_tabelog_rating}): {len(tabelog_restaurants)}")
    print(f"Omakase.in restaurants: {len(omakase_restaurants)}")
    print(f"Matched: {matched_count}")
    print(f"Unmatched: {len(matches) - matched_count}")
    print(f"\nWatchlist saved to: {WATCHLIST_PATH}")
    print(f"\nNext steps:")
    print(f"  1. Review and edit {WATCHLIST_PATH}")
    print(f"  2. Remove restaurants you don't want to monitor")
    print(f"  3. Add omakase_code for any manually found restaurants")
    print(f"  4. Run: python main.py --dry-run --no-headless")
    print()

    # Print matched restaurants
    print("Matched restaurants:")
    for m in matches:
        if m["omakase"]:
            t = m["tabelog"]
            o = m["omakase"]
            print(f"  [{t.tabelog_rating}] {t.name} -> {o.name} ({o.omakase_code}) [score: {m['score']}]")

    print("\nUnmatched restaurants (not found on omakase.in):")
    for m in matches:
        if not m["omakase"]:
            t = m["tabelog"]
            print(f"  [{t.tabelog_rating}] {t.name} (best score: {m['score']})")
