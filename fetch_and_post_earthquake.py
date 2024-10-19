from urllib.parse import quote
import requests
from datetime import datetime, timedelta, timezone
import os
import time
import re  # Import the regular expression module
from dotenv import load_dotenv

load_dotenv('.env.local')

def fetch_new_earthquakes():
    max_attempts = 5  
    attempt = 0

    # Get the current time once before the loop
    current_time = datetime.now(timezone.utc)

    while attempt < max_attempts:
        try:
            params = {
                'format': 'geojson',
                'starttime': (current_time - timedelta(minutes=5)).isoformat(),  
                'endtime': current_time.isoformat(),  
                'minmagnitude': 5.0,
            }

            response = requests.get('https://earthquake.usgs.gov/fdsnws/event/1/query', params=params)
            response.raise_for_status()  # Raise an error for bad responses
            
            data = response.json()
            new_earthquakes = []
            
            for feature in data['features']:
                if feature['properties']['type'] != 'earthquake':
                    print(f"found a non-earthquake type: {feature['properties']['type']}")
                    continue  

                print(f"found earthquake at time: {feature['properties']['time']}")
                new_earthquakes.append(feature)
            
            return new_earthquakes
        
        except requests.RequestException as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            attempt += 1  
            if attempt < max_attempts:  
                time.sleep(1)  
            if attempt == max_attempts:
                print("Max attempts reached. Giving up.")
                return []  

def post_to_threads(earthquakes):
    THREADS_USER_ID = os.getenv('THREADS_USER_ID')
    THREADS_ACCESS_TOKEN = os.getenv('THREADS_ACCESS_TOKEN')
    
    for earthquake in earthquakes:  
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
        google_maps_link = f"https://www.google.com/maps/place/{lat}+{lon}/@{lat},{lon},9z"
        usgs_link = earthquake['properties']['url']

        details_message = f" Details: {usgs_link}"

        if len(post_message) + len(details_message) <= 500:
            post_message += details_message

        print(post_message)

        THREADS_API_URL = (
            f"https://graph.threads.net/{THREADS_USER_ID}/threads?text={quote(post_message)}"
            f"&access_token={THREADS_ACCESS_TOKEN}&media_type=TEXT&link_attachment={quote(google_maps_link)}"
        )
        
        try:
            response = requests.post(THREADS_API_URL)
            response.raise_for_status()
            
            data = response.json()
            creation_id = data.get('id')  
            
            publish_url = f"https://graph.threads.net/{THREADS_USER_ID}/threads_publish?creation_id={creation_id}&access_token={THREADS_ACCESS_TOKEN}"
            publish_response = requests.post(publish_url)
            publish_response.raise_for_status()  
            
            print("Earthquake posted successfully.")
        except requests.RequestException as e:
            print(f"Failed to post earthquake: {e}")

if __name__ == "__main__":
    new_earthquakes = fetch_new_earthquakes()
    if new_earthquakes:
        post_to_threads(new_earthquakes)
    else:
        print("No new earthquakes found")
