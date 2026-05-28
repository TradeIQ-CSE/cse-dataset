import requests
import pandas as pd
import time
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def get_financial_reports(symbol):
    """Fetch PDF paths for annual and quarterly reports and check corporate profile for dividends."""
    url = 'https://www.cse.lk/api/financials'
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Try fetching financials
    try:
        r = requests.post(url, files={'symbol': (None, symbol)}, headers=headers, timeout=10)
        if r.status_code == 200:
            return symbol, r.json()
    except Exception as e:
        pass
        
    return symbol, None

def collect_financial_metadata():
    os.makedirs('data/raw/fundamentals', exist_ok=True)
    os.makedirs('data/processed/fundamentals', exist_ok=True)
    
    meta = pd.read_csv('data/processed/company_metadata.csv')
    active_symbols = meta[meta['delisted'] == False]['symbol'].tolist()
    
    logging.info(f"Fetching financial report metadata for {len(active_symbols)} companies...")
    
    reports = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_financial_reports, sym): sym for sym in active_symbols}
        for future in as_completed(futures):
            symbol, data = future.result()
            if data:
                if 'infoAnnualData' in data and data['infoAnnualData']:
                    for ann in data['infoAnnualData']:
                        manual_date = ann.get('manualDate')
                        year = pd.to_datetime(manual_date, unit='ms').year if manual_date else None
                        
                        reports.append({
                            'symbol': symbol,
                            'report_type': 'Annual',
                            'year': year,
                            'title': ann.get('fileText'),
                            'url': f"https://www.cse.lk/{ann.get('path')}" if ann.get('path') else None
                        })
                
                # We can also grab quarterly if needed, but per M4 we want Annuals for EPS/NAV
                
    if len(reports) > 0:
        df = pd.DataFrame(reports)
        df = df.dropna(subset=['url'])
        output_path = 'data/processed/fundamentals/annual_reports_index.csv'
        df.to_csv(output_path, index=False)
        logging.info(f"Saved {len(df)} annual report links to {output_path}")
    else:
        logging.warning("No annual report links found.")

if __name__ == '__main__':
    collect_financial_metadata()
