import json
import logging
import re

from curl_cffi import requests as cffi_requests

from models import AvailabilitySlot, Config, Restaurant

logger = logging.getLogger(__name__)

BASE_URL = "https://omakase.in"
AVAILABILITY_API = "/users/api/availability_dates"


class OmakaseScraper:
    """Pure HTTP scraper using curl_cffi to bypass CloudFlare. No browser needed."""

    def __init__(self, config: Config):
        self._config = config
        self._session: cffi_requests.Session | None = None

    def start(self):
        self._session = cffi_requests.Session(impersonate="chrome")

    def stop(self):
        if self._session:
            self._session.close()

    def login(self) -> bool:
        """Log in to omakase.in via curl_cffi. Returns True if successful."""
        logger.info("Logging in to omakase.in...")
        try:
            # Visit main page to get session cookie
            self._session.get(f"{BASE_URL}/en", timeout=15)

            # Get login page + CSRF token
            login_page = self._session.get(f"{BASE_URL}/en/users/sign_in", timeout=15)
            csrf_match = re.search(r'authenticity_token.*?value="([^"]+)"', login_page.text)
            if not csrf_match:
                logger.error("No CSRF token found on login page")
                return False

            # POST login
            resp = self._session.post(f"{BASE_URL}/en/users/sign_in", data={
                "authenticity_token": csrf_match.group(1),
                "user[email]": self._config.omakase_email,
                "user[password]": self._config.omakase_password,
                "user[remember_me]": "1",
            }, timeout=15, allow_redirects=True)

            if "/sign_in" in str(resp.url):
                logger.error("Login failed — still on sign_in page")
                return False

            logger.info("Login successful")
            return True

        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def check_restaurant(self, restaurant: Restaurant, target_dates: list[str]) -> list[AvailabilitySlot]:
        """
        Check availability via pure HTTP:
        1. GET /en/r/{slug}/reservations/new → extract reservableTill + token from React props
        2. GET /users/api/availability_dates → get available dates
        3. Match against target_dates
        """
        code = restaurant.omakase_code
        slots = []
        status = "closed"  # default

        try:
            # Step 1: Check if restaurant has reservation form
            resp = self._session.get(
                f"{BASE_URL}/en/r/{code}/reservations/new",
                timeout=15, allow_redirects=True
            )

            if "/reservations/new" not in str(resp.url):
                logger.info(f"  {restaurant.name}: not accepting reservations")
                return [], "closed"

            # Extract React props
            match = re.search(r'data-react-props="([^"]+)"', resp.text)
            if not match:
                logger.info(f"  {restaurant.name}: no reservation form found")
                return [], "closed"

            props_html = match.group(1).replace("&quot;", '"').replace("&amp;", "&")
            props = json.loads(props_html)
            reservable_till = props.get("reservableTill", "")
            token = props.get("reservationCalendarToken", "")

            if not token:
                logger.warning(f"  {restaurant.name}: no calendar token")
                return [], "closed"

            logger.info(f"  {restaurant.name}: OPEN, reservableTill={reservable_till}")

            # Step 2: Filter target dates within booking window
            bookable = [d for d in target_dates if d <= reservable_till]
            not_yet = [d for d in target_dates if d > reservable_till]

            if not_yet and not bookable:
                status = f"open till {reservable_till}"
                logger.info(f"  NOT YET OPEN: {', '.join(not_yet)} (window ends {reservable_till})")
                return [], status

            if not bookable:
                return [], f"open till {reservable_till}"

            # Step 3: Query availability API
            target_months = sorted(set(d[:7] for d in bookable))
            date_statuses = {}
            for ym in target_months:
                available = self._fetch_availability(code, ym, token)
                if available is None:
                    continue

                for td in bookable:
                    if td.startswith(ym) and td in available:
                        slots.append(AvailabilitySlot(
                            omakase_code=code,
                            restaurant_name=restaurant.name,
                            slot_date=td,
                            status="available",
                        ))
                        date_statuses[td] = "Y"
                        logger.info(f"  BOOKABLE: {td}")
                    elif td.startswith(ym):
                        date_statuses[td] = "N"
                        logger.info(f"  NO SLOTS: {td}")

            if slots:
                status = " ".join(f"{d[-5:]}={'Y' if d in [s.slot_date for s in slots] else 'N'}" for d in target_dates)
            else:
                status = f"open till {reservable_till}, no slots"

        except Exception as e:
            logger.error(f"Error checking {restaurant.name}: {e}")
            status = "error"

        return slots, status

    def _fetch_availability(self, slug: str, year_month: str, token: str) -> list[str] | None:
        """Fetch available dates via the API."""
        try:
            resp = self._session.get(
                f"{BASE_URL}{AVAILABILITY_API}"
                f"?restaurant_slug={slug}&year_month={year_month}"
                f"&reservation_calendar_token={token}",
                timeout=15,
            )
            data = resp.json().get("data", {})
            available = data.get("available_dates", [])
            logger.info(f"  API {slug}/{year_month}: {len(available)} available dates")
            return available
        except Exception as e:
            logger.error(f"  API error {slug}/{year_month}: {e}")
            return None

    def scrape_catalog(self) -> list[Restaurant]:
        """Scrape all restaurants from omakase.in listing pages."""
        restaurants = []
        for page_num in range(1, 25):
            url = f"{BASE_URL}/en/r" if page_num == 1 else f"{BASE_URL}/en/r/page/{page_num}"
            logger.info(f"Scraping catalog page {page_num}: {url}")

            resp = self._session.get(url, timeout=15)
            if resp.status_code != 200:
                break

            codes = re.findall(r'href="/en/r/([a-z]{2}\d{6})"', resp.text)
            names = re.findall(r'<h3[^>]*>\s*(.*?)\s*</h3>', resp.text)

            for i, code in enumerate(codes):
                name = names[i].strip() if i < len(names) else code
                restaurants.append(Restaurant(name=name, omakase_code=code))

            if f'page/{page_num + 1}' not in resp.text:
                break

        logger.info(f"Scraped {len(restaurants)} restaurants from catalog")
        return restaurants
