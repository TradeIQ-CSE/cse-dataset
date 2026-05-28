import requests
import pandas as pd
import time
from datetime import timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def fill_aspi_gap():
    # Gap identified from previous recon
    start_date = pd.to_datetime('2021-02-20')
    end_date = pd.to_datetime('2025-02-24')
    
    # We only need weekdays (trading days)
    date_range = pd.date_range(start=start_date, end=end_date, freq='B')
    logging.info(f"Attempting to fetch {len(date_range)} trading days via hidden API...")
    
    records = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = 'https://www.cse.lk/api/dailyMarketSummery'
    
    # Batch fetch to show progress, let's do the first 50 as a proof of concept
    # (If we do all ~1000 it will take a few minutes, so we'll test a chunk first)
    for dt in date_range[:50]:
        date_str = dt.strftime('%Y-%m-%d')
        try:
            r = requests.post(url, files={'date': (None, date_str)}, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data and isinstance(data, list) and len(data) > 0 and len(data[0]) > 0:
                    day_data = data[0][0]
                    # 'asi' is the All Share Price Index
                    aspi = day_data.get('asi')
                    if aspi:
                        records.append({
                            'Date': date_str,
                            'Open': aspi,
                            'High': aspi,
                            'Low': aspi,
                            'Close': aspi,
                            'Volume': day_data.get('volumeOfTurnOverNumber', 0)
                        })
                        print(f"  [+] {date_str}: ASPI = {aspi}")
                    else:
                        print(f"  [-] {date_str}: Market closed / no data")
                else:
                    print(f"  [-] {date_str}: Market closed / no data")
            else:
                print(f"  [!] {date_str}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  [!] {date_str}: Error {e}")
            
        time.sleep(0.2) # Polite rate limiting
        
    df = pd.DataFrame(records)
    print(f"\nSuccessfully fetched {len(df)} records!")
    print(df.head())
    
if __name__ == '__main__':
    fill_aspi_gap()
