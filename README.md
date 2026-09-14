# @earthquake.alerts

<img src="https://github.com/0xjessel/global-earthquake-bot/blob/main/images/profile_pic.png" alt="Profile Pic" width="300px">

Follow my [Threads profile](https://www.threads.net/@earthquake.alerts)!

# Overview

`fetch_and_post_earthquake.py` is run via a cron job on Dreamhost that is scheduled to run every 5 minutes.

`fetch_new_earthquakes()` gets the latest data from the USGS API.

`post_to_threads()` takes the earthquake data and calls the threads API to publish a post. I include the USGS link to the earthquake details plus a google maps link to the coordinates of the earthquake epicenter.

I also schedule `th_access_token.py` to be a cron job that's run every month to keep the access token valid.

# Notability gate (2026-09-13)

Posting is score-based instead of a plain magnitude floor. Every M4.0+ event in the fetch window is scored:

- **magnitude base:** M6.5+ = 100, M6.0-6.4 = 90, M5.5-5.9 = 70, M5.0-5.4 = 40, M4.5-4.9 = 20, M4.0-4.4 = 0
- **depth:** <10 km +25, <25 km +15, <50 km +5, >70 km -10 (depth is not in the USGS feed; fetched per event via a USGS QuakeML lookup, cached in state, skipped for M6.0+ which can never fall below the bar)
- **major-city proximity:** <100 km from a 1M+ city +25; 100-300 km from 1M+ or <100 km from a 500k-1M city +10

An event posts when its score is >= 60. **Region suppression:** if a post already came from within 300 km in the last 2 hours, the bar rises to 80. Skipped events are remembered (state `skipped`) and re-considered only if USGS revises the magnitude by 0.3+ (same event id). Backtest over 90 days (2,000 M4+ events, real depth lookups): ~100 posts / 90 days (~1.1/day) vs ~5.8/day under the old M5+ floor.

`major_cities.json` is a static GeoNames populated-places db (population >= 500k, ~1,210 cities). It is refreshed manually — there is intentionally **no cron / auto-refresh** for it.

# Instructions

1. **Clone the repository:**

   ```bash
   git clone https://github.com/0xjessel/global-earthquake-bot.git
   cd global-earthquake-bot
   ```

2. **Create a virtual environment:**

   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment:**

   - On Windows:
     ```bash
     venv\Scripts\activate
     ```
   - On macOS/Linux:
     ```bash
     source venv/bin/activate
     ```

4. **Install the required packages:**

   ```bash
   pip install -r requirements.txt
   ```

5. **Create a `.env.local` file:**

   - Copy the `.env.example` file to create your own environment configuration:
     ```bash
     cp .env.example .env.local
     ```

6. **Edit the `.env.local` file:**

   - Open `.env.local` in a text editor and fill in the required values

7. **Run the script**

```bash
python fetch_and_post_earthquake.py
```
