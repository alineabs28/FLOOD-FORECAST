import requests
import datetime
import os
import time
from tqdm import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from json.decoder import JSONDecodeError
import json
import pandas as pd
from prophet import Prophet
import schedule

# --- SETUP ---
API_KEY = os.getenv("GOOGLE_FLOOD_API_KEY", "YOUR_API_KEY_HERE")
page_size = 10000
GAUGE_BUCKET_SIZE = 500
DAYS_HISTORY = 30
DAYS_FUTURE = 7

country_codes = [
    "AO","AR","AT","AU","AZ","BD","BE","BF","BG","BO","BR","BW","BY","BZ","CA","CD",
    "CF","CG","CH","CI","CL","CM","CO","CR","CZ","DE","DK","EC","EE","ES","FI","FR",
    "GB","GE","GH","GM","GN","GR","GT","GW","GY","HN","HU","ID","IE","IN","IS","IT",
    "KE","KG","KH","KZ","LA","LK","LR","LS","LT","LV","MD","MG","ML","MM","MW","MX",
    "MY","MZ","NA","NG","NI","NL","NO","NP","NZ","PE","PG","PH","PK","PL","PT","PY",
    "RO","RS","RW","SE","SI","SK","SL","SN","SO","SR","SS","TD","TH","TJ","TR","UA",
    "US","UY","UZ","VE","VN","ZA","ZM","ZW"
]

# --- SESSION SETUP ---
session = requests.Session()
retry_strategy = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# --- STEP 1: FETCH GAUGES ---
def fetch_gauges():
    all_gauges = []
    print("--- Fetching gauges for all countries ---")
    for country_code in tqdm(country_codes, desc="Processing countries"):
        request_payload = {"regionCode": country_code, "pageSize": page_size, "includeNonQualityVerified": False}
        while True:
            try:
                res_json = session.post(
                    f'https://floodforecasting.googleapis.com/v1/gauges:searchGaugesByArea?key={API_KEY}',
                    json=request_payload, timeout=30
                ).json()
                if 'error' in res_json:
                    print(f"API Error for {country_code}: {res_json['error']}")
                    break
                if 'gauges' in res_json:
                    for gauge in res_json['gauges']:
                        if 'location' in gauge and 'latitude' in gauge['location'] and 'longitude' in gauge['location']:
                            all_gauges.append(gauge)
                        else:
                            print(f"WARNING: Gauge {gauge.get('gaugeId', 'Unknown')} missing coordinates, skipping.")
                if not res_json.get('nextPageToken'):
                    break
                request_payload['pageToken'] = res_json['nextPageToken']
                time.sleep(0.5)
            except (requests.exceptions.RequestException, JSONDecodeError) as e:
                print(f"\nERROR: Could not fetch data for {country_code}. Error: {e}")
                break
    print(f"\n✅ Total gauges fetched: {len(all_gauges)}")
    return all_gauges

# --- STEP 2: FETCH FORECASTS ---
def fetch_forecasts(all_gauges):
    forecasts = {}
    if not all_gauges: return forecasts
    gauge_ids = [g['gaugeId'] for g in all_gauges]
    gauge_id_buckets = [gauge_ids[i:i + GAUGE_BUCKET_SIZE] for i in range(0, len(gauge_ids), GAUGE_BUCKET_SIZE)]
    last_days = datetime.datetime.utcnow() - datetime.timedelta(days=DAYS_HISTORY)
    now = datetime.datetime.utcnow()
    print("\n--- Fetching forecasts for all gauges ---")
    for gauge_bucket in tqdm(gauge_id_buckets, desc="Forecast batches"):
        try:
            params = {
                'key': API_KEY,
                'gaugeIds': gauge_bucket,
                'issuedTimeStart': last_days.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'issuedTimeEnd': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            res = session.get('https://floodforecasting.googleapis.com/v1/gauges:queryGaugeForecasts', params=params, timeout=60)
            res_json = res.json()
            if res_json and 'forecasts' in res_json:
                forecasts.update(res_json['forecasts'])
            time.sleep(1)
        except JSONDecodeError:
            print(f"INFO: Empty or invalid response for batch")
            continue
        except requests.exceptions.RequestException as e:
            print(f"ERROR: Could not fetch forecast batch. {e}")
            continue
    print(f"\n✅ Total gauges with forecasts: {len(forecasts)}")
    return forecasts

# --- STEP 3: PROPHET PREDICTION ---
def prophet_forecast(gauge_forecast):
    values = []
    for f in gauge_forecast.get('forecasts', []):
        for r in f.get('forecastRanges', []):
            if 'timestamp' in r and 'value' in r:
                values.append({'ds': r['timestamp'], 'y': r['value']})
    
    if not values or len(values) < 5: 
        return [] 
        
    df = pd.DataFrame([{'ds': datetime.datetime.strptime(v['ds'], "%Y-%m-%dT%H:%M:%SZ"), 'y': float(v['y'])} for v in values])

    if df['ds'].nunique() < 2:
        return []

    try:
        m = Prophet(daily_seasonality=True)
        m.fit(df)
        future = m.make_future_dataframe(periods=DAYS_FUTURE)
        forecast = m.predict(future)
        predicted = forecast[['ds','yhat']].tail(DAYS_FUTURE).to_dict('records')
        return predicted
    except Exception as e:
        print(f"Prophet prediction failed for a gauge: {e}") 
        return []

# --- STEP 4: MERGE GAUGES + FORECASTS + PREDICTION ---
def merge_and_save(all_gauges, forecasts):
    combined = []
    forecast_gauge_ids = set(forecasts.keys())
    process_gauges = [g for g in all_gauges if g['gaugeId'] in forecast_gauge_ids]
    
    print("\n--- Generating Prophet predictions and combining data ---")
    for gauge in tqdm(process_gauges, desc="Building final JSON"):
        gid = gauge['gaugeId']
        predicted = prophet_forecast(forecasts[gid]) 
        
        combined.append({
            "gaugeId": gid,
            "lat": gauge['location']['latitude'],
            "lon": gauge['location']['longitude'],
            "siteName": gauge.get('siteName', ''),
            "river": gauge.get('river', ''),
            "source": gauge.get('source', ''),
            "qualityVerified": gauge.get('qualityVerified', False),
            "hasModel": gauge.get('hasModel', False),
            "forecasts": forecasts.get(gid, {}).get('forecasts', []) or [],
            "predicted_next_7days": predicted or []
        })
        
    output_file = "flood_full_data.json"
    with open(output_file, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"\n✅ Saved all data (past + now + predicted) to '{output_file}'")

# --- STEP 5: RUN ALL ---
def run_all():
    gauges = fetch_gauges()
    forecasts = fetch_forecasts(gauges)
    merge_and_save(gauges, forecasts)

# --- AUTO-RUN EVERY 24 HOURS ---
if __name__ == "__main__":
    run_all()
    print(f"\n--- Scheduling next run in 24 hours from Oct 9, 2025, 08:15 PM EDT ---")
    schedule.every(24).hours.do(run_all)
    while True:
        schedule.run_pending()
        time.sleep(60)