import requests
import pandas as pd
import time
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def fetch_corporate_calendar(year, month):
    url = 'https://www.cse.lk/api/corporateCalender'
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        # Based on testing, data dict with year and month works
        r = requests.post(url, data={'year': str(year), 'month': str(month)}, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get('approvedCoopCalAnnouncements', [])
    except Exception as e:
        logging.error(f"Error fetching {year}-{month}: {e}")
    return []


def parse_event_date(value):
    if pd.isna(value):
        return pd.NaT
    if isinstance(value, (int, float)) and value > 10_000:
        return pd.to_datetime(value, unit='ms', errors='coerce')
    return pd.to_datetime(value, errors='coerce')


def parse_dividend_amount(row):
    text_parts = []
    for col in ['remarks', 'announcementCategory', 'type']:
        value = row.get(col)
        if pd.notna(value):
            text_parts.append(str(value))
    text = ' '.join(text_parts)

    patterns = [
        r'(?:rs\.?|lkr)\s*[:/-]?\s*([0-9]+(?:\.[0-9]+)?)',
        r'([0-9]+(?:\.[0-9]+)?)\s*(?:per share|per ordinary share|per voting share|per non-voting share)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1)), 'LKR', 'parsed_text', 'parsed'

    if text.strip():
        return pd.NA, pd.NA, 'text_available', 'not_found'
    return pd.NA, pd.NA, 'no_text_fields', 'not_found'


def normalize_dividends(dividends):
    columns = [
        'symbol', 'company', 'announcementCategory', 'dateOfAnnouncement',
        'recordDate', 'xd', 'paymentDate', 'announcementId',
        'amount_per_share', 'currency', 'amount_source', 'amount_parse_status',
    ]
    if dividends.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for _, row in dividends.iterrows():
        amount, currency, source, status = parse_dividend_amount(row)
        rows.append({
            'symbol': row.get('symbol'),
            'company': row.get('company'),
            'announcementCategory': row.get('announcementCategory'),
            'dateOfAnnouncement': parse_event_date(row.get('dateOfAnnouncement')),
            'recordDate': parse_event_date(row.get('recordDate')),
            'xd': parse_event_date(row.get('xd')),
            'paymentDate': parse_event_date(row.get('paymentDate')),
            'announcementId': row.get('announcementId'),
            'amount_per_share': amount,
            'currency': currency,
            'amount_source': source,
            'amount_parse_status': status,
        })
    out = pd.DataFrame(rows, columns=columns)
    for col in ['dateOfAnnouncement', 'recordDate', 'xd', 'paymentDate']:
        out[col] = pd.to_datetime(out[col], errors='coerce').dt.strftime('%Y-%m-%d')
    return out

def collect_corporate_actions():
    os.makedirs('data/raw/fundamentals', exist_ok=True)
    os.makedirs('data/processed/fundamentals', exist_ok=True)
    
    # We will fetch data from 2010 to 2026.
    years = range(2010, 2027)
    months = range(1, 13)
    
    tasks = [(y, m) for y in years for m in months]
    logging.info(f"Fetching corporate actions for {len(tasks)} months...")
    
    all_events = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_corporate_calendar, y, m): (y, m) for (y, m) in tasks}
        
        for future in as_completed(futures):
            events = future.result()
            if events:
                all_events.extend(events)
                
    logging.info(f"Found {len(all_events)} total corporate actions.")
    
    if len(all_events) == 0:
        return
        
    df = pd.DataFrame(all_events)
    
    # Drop duplicates just in case there's overlap in the API responses
    if 'id' in df.columns:
        df = df.drop_duplicates(subset=['id'])
    
    df.to_csv('data/raw/fundamentals/all_corporate_actions_raw.csv', index=False)
    
    # Filter for dividends and splits (Subdivision of shares)
    dividends = df[df['announcementCategory'].str.contains('DIVIDEND', na=False, case=False)]
    splits = df[df['announcementCategory'].str.contains('SUBDIVISION|SPLIT', na=False, case=False)]
    
    logging.info(f"Filtered {len(dividends)} dividends and {len(splits)} splits.")
    
    normalize_dividends(dividends).to_csv('data/processed/fundamentals/dividends.csv', index=False)
    splits.to_csv('data/processed/fundamentals/splits.csv', index=False)
    
if __name__ == '__main__':
    collect_corporate_actions()
