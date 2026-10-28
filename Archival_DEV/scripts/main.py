import asyncio
import json
import logging
import os
from pathlib import Path
import re
import httpx
from tqdm.asyncio import tqdm

CONCURRENCY_LIMIT = 5  # Concurrent async workers
REQUEST_DELAY = 0.15   # Delay between requests per worker (in seconds)
METADATA_DIR = Path("metadata")
MODEL_IDS_FILE = Path("model_ids.txt")
COOKIES_FILE = Path("cookies.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("api_fetch.log", mode="w", encoding="utf-8")],
)
logger = logging.getLogger("mmf_api")


def load_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {}

    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        raw_cookies = json.load(f)

    return {
        c["name"]: c["value"]
        for c in raw_cookies
        if "name" in c and "value" in c
    }


async def fetch_model_metadata(
    client: httpx.AsyncClient,
    model_id: str,
    semaphore: asyncio.Semaphore,
) -> bool:
    output_file = METADATA_DIR / f"model_{model_id}.json"
    if output_file.exists() and output_file.stat().st_size > 0:
        return True

    url = f"https://www.myminifactory.com/api/v2/objects/{model_id}"

    async with semaphore:
        for attempt in range(1, 4):
            try:
                response = await client.get(url, timeout=20.0)

                if response.status_code == 200:
                    data = response.json()
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    await asyncio.sleep(REQUEST_DELAY)
                    return True

                if response.status_code == 429:
                    wait_time = int(response.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited on {model_id}. Sleeping {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code in (403, 404):
                    logger.warning(f"HTTP {response.status_code} on {url}: {response.text[:120]}")
                    return False

            except Exception as e:
                logger.error(f"Error fetching {model_id} (attempt {attempt}/3): {e}")
                await asyncio.sleep(1.5 * attempt)

        return False


async def main():
    if not MODEL_IDS_FILE.exists():
        print(f"Error: {MODEL_IDS_FILE} not found.")
        return

    METADATA_DIR.mkdir(exist_ok=True)
    cookies = load_cookies()

    # Load and clean IDs to digits only
    with open(MODEL_IDS_FILE, "r", encoding="utf-8") as f:
        all_ids = [re.sub(r"\D", "", line.strip()) for line in f if line.strip()]
        all_ids = [mid for mid in all_ids if mid]

    # Filter out models already fetched on disk
    pending_ids = [
        mid for mid in all_ids
        if not (METADATA_DIR / f"model_{mid}.json").exists()
        or (METADATA_DIR / f"model_{mid}.json").stat().st_size == 0
    ]

    print(f"Total model IDs: {len(all_ids)}")
    print(f"Already fetched: {len(all_ids) - len(pending_ids)}")
    print(f"Pending fetch:   {len(pending_ids)}")

    if not pending_ids:
        print("All metadata files have already been fetched.")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.myminifactory.com/library",
    }

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async with httpx.AsyncClient(headers=headers, cookies=cookies, follow_redirects=True) as client:
        tasks = [fetch_model_metadata(client, mid, semaphore) for mid in pending_ids]
        results = await tqdm.gather(*tasks, desc="Fetching Metadata", unit="model")

    successful = sum(1 for r in results if r)
    print(f"\nCompleted! Saved {successful}/{len(pending_ids)} new metadata files.")
    print(f"Total saved files in '{METADATA_DIR}/': {len(list(METADATA_DIR.glob('model_*.json')))}")


if __name__ == "__main__":
    asyncio.run(main())
