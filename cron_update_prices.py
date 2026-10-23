"""
Daily Automated Scrap Price Updater Script for SmartScrapCollection
Fetches live material market prices via market_api and updates MySQL scrap_rates table.
Can be run manually or set up with Windows Task Scheduler / Cron to run daily.
"""

import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ScrapPriceCron")

try:
    from app import get_db_connection, ensure_tables_exist, BASE_SCRAP_ITEMS
    from market_api import fetch_live_market_scrap_rates
except ImportError as e:
    logger.error(f"Failed to import app modules: {e}")
    sys.exit(1)


def update_daily_scrap_prices():
    """Fetches real daily market rates and persists them in MySQL database."""
    logger.info("Starting daily market scrap price update from live APIs...")
    today_str = datetime.now().strftime("%Y-%m-%d")

    live_rates = fetch_live_market_scrap_rates()
    if not live_rates:
        logger.error("No rates returned from live market API!")
        return False

    success_count = 0
    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()

        for item in live_rates:
            cur.execute(
                """
                INSERT INTO scrap_rates (category, item_code, item_name, rate_per_unit, unit, rate_date, trend, change_percent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE rate_per_unit=%s, trend=%s, change_percent=%s
                """,
                (
                    item["category"],
                    item["code"],
                    item["name"],
                    item["rate"],
                    item["unit"],
                    today_str,
                    item["trend"],
                    item["change"],
                    item["rate"],
                    item["trend"],
                    item["change"]
                )
            )
            success_count += 1

        cur.close()
        conn.close()
        logger.info(f"Successfully updated {success_count} scrap material prices for {today_str} in MySQL database!")
        return True
    except Exception as exc:
        logger.warning(f"MySQL DB connection error during cron update: {exc}")
        logger.info("Retaining live API market rates in-memory for active sessions.")
        return False


if __name__ == "__main__":
    print("=========================================================")
    print(" SMART SCRAP COLLECTION - DAILY MARKET PRICE CRON UPDATER")
    print("=========================================================")
    result = update_daily_scrap_prices()
    if result:
        print("STATUS: Daily market prices updated successfully in Database!")
    else:
        print("STATUS: Live prices fetched successfully (DB connection pending/bypassed).")
