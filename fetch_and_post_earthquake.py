from urllib.parse import quote
import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import re  # Import the regular expression module
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv

load_dotenv('.env.local')

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'earthquake_state.json')
STATE_MAX_AGE = timedelta(hours=24)
DRY_RUN = False


def load_state():
    """Return the state dict, or None if the file does not exist yet.

    A corrupt file is treated as empty (bootstrap happens on that run)
    rather than crashing every 5 minutes.
    """
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as e:
        print(f"WARNING: could not read state file {STATE_FILE} ({e}); starting with empty state")
        return {'seen': {}}
    if not isinstance(state, dict) or not isinstance(state.get('seen'), dict):
        print(f"WARNING: state file {STATE_FILE} has unexpected shape; starting with empty state")
        return {'seen': {}}
    return state


def save_state(state):
    """Atomically write the state file: temp file in same dir, fsync, replace. Mode 600."""
    d = os.path.dirname(STATE_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix='earthquake_state.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), 0o600)
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def prune_seen(seen, now):
    """Drop seen entries older than STATE_MAX_AGE. Returns number pruned."""
    pruned = 0
    for fid in list(seen.keys()):
        ts = seen[fid]
        try:
            stored = datetime.fromisoformat(ts)
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=timezone.utc)
            if now - stored > STATE_MAX_AGE:
                del seen[fid]
                pruned += 1
        except (ValueError, TypeError):
            print(f"WARNING: unparseable timestamp for seen id {fid}; keeping it")
    return pruned


def fetch_new_earthquakes():
    """Fetch recent global USGS earthquakes (minmagnitude 5.0).

    Returns a list of feature dicts, or None on a definitive failure
    (4xx or exhausted retries). Never raises.
    """
    max_attempts = 4
    timeout_s = 4
    sleep_s = 2

    # Get the current time once before the loop
    current_time = datetime.now(timezone.utc)
    params = {
        'format': 'geojson',
        'updatedafter': (current_time - timedelta(minutes=30)).isoformat(),
        'minmagnitude': 5.0,
    }

    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get('https://earthquake.usgs.gov/fdsnws/event/1/query',
                                    params=params, timeout=timeout_s)
            if 400 <= response.status_code < 500:
                print(f"HTTP {response.status_code} (client error) from USGS; not retrying. "
                      f"Body snippet: {response.text[:200]!r}")
                return None
            response.raise_for_status()  # Raise an error for bad responses
            break
        except requests.RequestException as e:
            if attempt == max_attempts:
                print(f"Error fetching USGS after {max_attempts} attempts: {e}")
                return None
            print(f"Fetch attempt {attempt} failed ({e}). Retrying in {sleep_s}s...")
            time.sleep(sleep_s)

    try:
        data = response.json()
    except ValueError as e:
        print(f"ERROR: USGS response is not valid JSON ({e}). Snippet: {response.text[:200]!r}")
        return None
    if not isinstance(data, dict):
        print(f"ERROR: unexpected USGS response shape (not an object). Snippet: {response.text[:200]!r}")
        return None

    new_earthquakes = []
    for feature in data.get('features', []):
        if feature['properties']['type'] != 'earthquake':
            print(f"found a non-earthquake type: {feature['properties']['type']}")
            continue

        magnitude = feature['properties']['mag']
        if magnitude is None:
            print(f"Skipping event with null magnitude: id={feature.get('id')}, "
                  f"place={feature['properties'].get('place')}")
            continue

        print(f"earthquake occurred: {datetime.fromtimestamp(feature['properties']['time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"earthquake updated: {datetime.fromtimestamp(feature['properties']['updated'] / 1000).strftime('%Y-%m-%d %H:%M:%S')}")

        new_earthquakes.append(feature)

    return new_earthquakes


def build_post_message(earthquake):
    """Build the Threads post text (and maps link) for an earthquake feature."""
    magnitude = f"{round(earthquake['properties']['mag'], 1):.1f}"  # Format inline to one decimal place
    location = earthquake['properties']['place']
    coordinates = earthquake['geometry']['coordinates']
    lat, lon = coordinates[1], coordinates[0]  # USGS returns [lon, lat]

    # Convert km to mi
    match = re.match(r"(\d+)\s*km", location)
    if match:
        km_value = float(match.group(1))
        miles_value = round(km_value * 0.621371, 1)  # Convert km to miles and round

        miles_value_str = f"{int(miles_value)}" if miles_value.is_integer() else f"{miles_value:.1f}"

        mile_string = "mile" if miles_value < 1 else "miles"
        location = f"{miles_value_str} {mile_string} {location[match.end():].strip()}"
    else:
        location = f"in {location}"

    post_message = f"{magnitude} magnitude earthquake occurred {location}."
    google_maps_link = f"https://www.google.com/maps/place/{lat}+{lon}/@{lat},{lon},4z"
    usgs_link = earthquake['properties']['url']

    details_message = f" Details: {usgs_link}"

    if len(post_message) + len(details_message) <= 500:
        post_message += details_message

    return post_message, google_maps_link


def post_single_to_threads(post_message, google_maps_link):
    """Post one earthquake to Threads. Returns True only on full success.

    Uses Authorization: Bearer header (token no longer in the URL query).
    Retries once on failure before giving up.
    """
    THREADS_USER_ID = os.getenv('THREADS_USER_ID')
    THREADS_ACCESS_TOKEN = os.getenv('THREADS_ACCESS_TOKEN')
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        print("ERROR: THREADS_USER_ID or THREADS_ACCESS_TOKEN not set; cannot post.")
        return False

    THREADS_API_URL = (
        f"https://graph.threads.net/{THREADS_USER_ID}/threads"
        f"?text={quote(post_message)}&media_type=TEXT&link_attachment={quote(google_maps_link)}"
    )
    headers = {'Authorization': f'Bearer {THREADS_ACCESS_TOKEN}'}

    for attempt in (1, 2):
        try:
            response = requests.post(THREADS_API_URL, headers=headers, timeout=4)
            response.raise_for_status()

            data = response.json()
            creation_id = data.get('id') if isinstance(data, dict) else None
            if not creation_id:
                print(f"ERROR: Threads creation response missing 'id': {response.text[:200]!r}")
                break

            publish_url = (
                f"https://graph.threads.net/{THREADS_USER_ID}/threads_publish"
                f"?creation_id={creation_id}"
            )
            publish_response = requests.post(publish_url, headers=headers, timeout=4)
            publish_response.raise_for_status()

            print("Earthquake posted successfully.")
            return True
        except requests.RequestException as e:
            print(f"Failed to post earthquake (attempt {attempt}): {e}")
            if attempt == 1:
                time.sleep(2)
    return False


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="Fetch recent global earthquakes (M5+) and post new ones to Threads.")
    ap.add_argument('--dry-run', action='store_true',
                    help="Do everything except the Threads POST calls; do not record posts as seen")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    if DRY_RUN:
        print("=== DRY RUN: no posts to Threads will be made ===")

    now = datetime.now(timezone.utc)

    state = load_state()
    earthquakes = fetch_new_earthquakes()
    if earthquakes is None:
        print("Fetch failed; skipping this run. State file unchanged.")
        return

    # First-run bootstrap: mark everything currently in the window as seen WITHOUT posting.
    if state is None:
        state = {'seen': {}}
        for eq in earthquakes:
            fid = eq.get('id')
            if fid:
                state['seen'][fid] = now.isoformat()
        save_state(state)
        print(f"BOOTSTRAP: state file did not exist; initialized with {len(state['seen'])} "
              f"current earthquake ids (NOT posted)")
        print("No earthquakes posted (bootstrap run).")
        return

    pruned = prune_seen(state['seen'], now)
    if pruned:
        print(f"Pruned {pruned} seen entries older than 24h.")

    failed = state.setdefault('failed', {})
    for fid in list(failed.keys()):
        keep = False
        try:
            first = datetime.fromisoformat(failed[fid]['first'])
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            keep = (now - first) <= STATE_MAX_AGE
        except (ValueError, TypeError, KeyError):
            keep = False
        if not keep:
            del failed[fid]

    seen = state['seen']
    to_post = [eq for eq in earthquakes if eq.get('id') and eq['id'] not in seen]

    if not to_post:
        print("No new earthquakes found")
        return

    if DRY_RUN:
        for eq in to_post:
            post_message, _ = build_post_message(eq)
            print(f"[DRY-RUN] WOULD POST: {post_message}")
        print(f"[DRY-RUN] {len(to_post)} earthquake(s) would be posted; no posts made, seen-set not updated for them.")
        return

    posted_ids = []
    for eq in to_post:
        post_message, google_maps_link = build_post_message(eq)
        print(post_message)
        if post_single_to_threads(post_message, google_maps_link):
            posted_ids.append(eq['id'])

    failed_ids = [eq['id'] for eq in to_post if eq['id'] not in posted_ids]

    for fid in posted_ids:
        seen[fid] = now.isoformat()
        if fid in failed:
            del failed[fid]
            print(f"Posting succeeded for {fid}; clearing retry record.")

    for fid in failed_ids:
        rec = failed.get(fid)
        if rec is None:
            rec = {'attempts': 0, 'first': now.isoformat()}
            failed[fid] = rec
        rec['attempts'] += 1
        try:
            first = datetime.fromisoformat(rec['first'])
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            age = now - first
        except (ValueError, TypeError):
            first = now
            age = timedelta(0)
        if rec['attempts'] >= 3 or age >= timedelta(minutes=30):
            seen[fid] = now.isoformat()
            del failed[fid]
            print(f"Giving up on {fid} after {rec['attempts']} attempt(s) over {age}; marked as seen.")

    if posted_ids or failed_ids:
        save_state(state)
        if posted_ids:
            print(f"State updated; now tracking {len(seen)} seen ids.")
        if failed:
            print(f"Pending retry next run: {sorted(failed.keys())}")
        elif failed_ids:
            print("All failed posts resolved; nothing pending.")
    else:
        print("No earthquakes posted successfully; state file not updated (will retry next run).")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print("UNEXPECTED ERROR:")
        traceback.print_exc()
        sys.exit(0)
