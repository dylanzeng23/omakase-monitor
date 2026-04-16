#!/usr/bin/env python3
"""Omakase.in Restaurant Availability Monitor — monitors top-rated Tokyo restaurants and sends Telegram alerts."""

import argparse
import asyncio
import logging
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from functools import partial
from pathlib import Path

import yaml

import db
from models import AvailabilitySlot, Config, Restaurant, RunLog
from notifier import (
    send_alerts, send_message, build_bot_app, set_search_callback,
    set_watchlist, set_watchlist_path, is_search_requested,
)
from scrapers.omakase import OmakaseScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "omakase_monitor.log"),
    ],
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> Config:
    config_path = Path(__file__).parent / path
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return Config.from_yaml(data)


def load_watchlist(path: str = "watchlist.yaml") -> list[Restaurant]:
    watchlist_path = Path(__file__).parent / path
    if not watchlist_path.exists():
        logger.warning(f"Watchlist not found: {watchlist_path}")
        return []
    with open(watchlist_path) as f:
        data = yaml.safe_load(f)
    restaurants = []
    for r in data.get("restaurants", []):
        if not r.get("omakase_code"):
            continue
        restaurants.append(Restaurant(
            name=r["name"],
            omakase_code=r["omakase_code"],
            tabelog_rating=r.get("tabelog_rating", 0.0),
            tabelog_url=r.get("tabelog_url", ""),
            cuisine=r.get("cuisine", ""),
            location=r.get("location", "Tokyo"),
            notes=r.get("notes", ""),
        ))
    return restaurants


async def run_search_cycle(config: Config, watchlist: list[Restaurant], dry_run: bool = False) -> list[AvailabilitySlot]:
    """Run one full search cycle across all restaurants in the watchlist."""
    all_new_slots = []

    run_log = RunLog(started_at=datetime.now(UTC))
    log_id = db.save_run_log(run_log)

    def _do_search():
        scraper = OmakaseScraper(config)
        scraper.start()
        try:
            if not scraper.login():
                return None, "Login failed"
            results = []
            restaurants = list(watchlist)
            random.shuffle(restaurants)
            for restaurant in restaurants:
                try:
                    slots, status = scraper.check_restaurant(restaurant, config.target_dates)
                    results.append((restaurant, slots, status))
                except Exception as e:
                    logger.error(f"Error checking {restaurant.name}: {e}")
                    results.append((restaurant, [], "error"))
            return results, None
        finally:
            scraper.stop()

    search_results, err = await asyncio.to_thread(_do_search)

    if err:
        run_log.status = "error"
        run_log.error_message = err
        run_log.finished_at = datetime.now(UTC)
        db.update_run_log(log_id, run_log)
        return []

    for restaurant, slots, status in search_results:
        run_log.restaurants_checked += 1
        run_log.slots_found += len(slots)
        for slot in slots:
            is_new = db.save_result(slot)
            if is_new:
                all_new_slots.append(slot)
                logger.info(f"NEW: {slot.restaurant_name} {slot.slot_date} {slot.slot_time}")

    run_log.status = "success"
    run_log.new_slots = len(all_new_slots)
    run_log.finished_at = datetime.now(UTC)
    db.update_run_log(log_id, run_log)

    if all_new_slots and not dry_run:
        await send_alerts(config, all_new_slots)
        for slot in all_new_slots:
            db.mark_notified(slot)

    return all_new_slots


async def run_immediate_search(config: Config, watchlist: list[Restaurant]) -> tuple[list[AvailabilitySlot], list[dict]]:
    """Run an immediate search (called from Telegram bot). Returns (slots, report)."""
    def _do_immediate():
        scraper = OmakaseScraper(config)
        scraper.start()
        try:
            if not scraper.login():
                return [], [{"name": "LOGIN FAILED", "status": "error"}]
            all_slots = []
            report = []
            for restaurant in watchlist:
                try:
                    slots, status = scraper.check_restaurant(restaurant, config.target_dates)
                    all_slots.extend(slots)
                    for slot in slots:
                        db.save_result(slot)
                    report.append({
                        "name": restaurant.name,
                        "rating": restaurant.tabelog_rating,
                        "cuisine": restaurant.cuisine,
                        "status": status,
                    })
                except Exception as e:
                    logger.error(f"Immediate search error for {restaurant.name}: {e}")
                    report.append({"name": restaurant.name, "rating": restaurant.tabelog_rating,
                                   "cuisine": restaurant.cuisine, "status": "error"})
            return all_slots, report
        finally:
            scraper.stop()

    return await asyncio.to_thread(_do_immediate)


async def scheduler_loop(config: Config, watchlist: list[Restaurant]):
    """Main loop that runs searches on a schedule and listens for bot commands."""
    interval_seconds = config.interval_minutes * 60
    logger.info(f"Scheduler started. Interval: {config.interval_minutes}min ({interval_seconds:.0f}s)")

    # Set up search callback for Telegram bot
    async def search_callback():
        return await run_immediate_search(config, watchlist)

    set_search_callback(search_callback)
    set_watchlist(watchlist)
    set_watchlist_path(Path(__file__).parent / "watchlist.yaml")

    # Start Telegram bot polling
    bot_app = None
    if config.bot_token and config.chat_id:
        bot_app = build_bot_app(config)
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started")
        await send_message(
            config,
            "Omakase Monitor started.\n\n"
            "Commands:\n"
            "  /check - Run now\n"
            "  /status - Last run info\n"
            "  /list - Watched restaurants\n"
            "  /recent - Recent finds\n"
            "  /dates - Target dates\n"
            "  /add code Name - Add restaurant\n"
            "  /remove Name - Remove restaurant\n"
            f"\nMonitoring {len(watchlist)} restaurants every {config.interval_minutes}min\n"
            f"Target dates: {', '.join(config.target_dates)}"
        )

    try:
        while True:
            logger.info("Starting scheduled search cycle...")
            try:
                new_slots = await run_search_cycle(config, watchlist)
                logger.info(f"Search cycle complete. {len(new_slots)} new slots.")
            except Exception as e:
                logger.error(f"Search cycle failed: {e}")

            # Wait for next cycle, checking for manual trigger every 10s
            elapsed = 0
            while elapsed < interval_seconds:
                await asyncio.sleep(10)
                elapsed += 10
                if is_search_requested():
                    logger.info("Immediate search requested via Telegram")
                    break
    finally:
        if bot_app:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


async def run_discovery(config: Config):
    """Run Phase 1: discovery mode."""
    from discovery import run_discovery as discover
    await discover(config)


async def main():
    parser = argparse.ArgumentParser(description="Omakase.in Restaurant Availability Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Run one cycle without sending alerts")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--discover", action="store_true", help="Run Phase 1 discovery (Tabelog + omakase.in catalog)")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--headless", action="store_true", default=None, help="Force headless mode")
    parser.add_argument("--no-headless", action="store_true", help="Force non-headless mode (visible browser)")
    args = parser.parse_args()

    config = load_config(args.config)

    # Override headless from CLI
    if args.headless:
        config.headless = True
    elif args.no_headless:
        config.headless = False

    db.init_db()

    if args.discover:
        await run_discovery(config)
        return

    watchlist = load_watchlist()
    if not watchlist:
        logger.error("No restaurants in watchlist. Run --discover first or create watchlist.yaml manually.")
        return

    logger.info(f"Loaded {len(watchlist)} restaurants")
    for r in watchlist:
        rating = f" ({r.tabelog_rating})" if r.tabelog_rating else ""
        logger.info(f"  {r.name}{rating} [{r.omakase_code}]")
    logger.info(f"Target dates: {config.target_dates}")

    if args.dry_run or args.once:
        slots = await run_search_cycle(config, watchlist, dry_run=args.dry_run)
        print(f"\nFound {len(slots)} new slot(s):")
        for s in slots:
            time_str = f" {s.slot_time}" if s.slot_time else ""
            print(f"  {s.restaurant_name} - {s.slot_date}{time_str}")
    else:
        await scheduler_loop(config, watchlist)


if __name__ == "__main__":
    asyncio.run(main())
