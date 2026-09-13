#!/bin/bash
cd /home/meltedxice/cron_jobs/global-earthquake-bot

source venv/bin/activate

echo "========== [$(date +"%Y-%m-%d %H:%M:%S")] ==========" >> /home/meltedxice/cron_jobs/global-earthquake-bot/earthquakes.log

/home/meltedxice/cron_jobs/global-earthquake-bot/venv/bin/python fetch_and_post_earthquake.py >> /home/meltedxice/cron_jobs/global-earthquake-bot/earthquakes.log 2>&1
