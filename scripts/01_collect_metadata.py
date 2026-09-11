import requests
import pandas as pd
import time
import logging
import os
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

REQUEST_TIMEOUT_SECONDS = int(os.getenv("CSE_METADATA_TIMEOUT_SECONDS", "15"))
REQUEST_SLEEP_SECONDS = float(os.getenv("CSE_METADATA_SLEEP_SECONDS", "0.1"))

# Security classes that can appear in the daily price snapshot, keyed by the
# letter after the dot. Units (.U) and rights lines (.R) trade in tradeSummary;
# leaving rights out rejected 17 days of 2026 prices over one or two rows each.
# Debentures (.D) are left out.
SHARE_TYPES = {
    'N': 'Voting',
    'X': 'Non-Voting',
    'U': 'Unit',
    'R': 'Rights',
    'P': 'Preference',
    'W': 'Warrant',
}


def share_class(symbol):
    """The class letter of a symbol such as HNBF.R0000, or '' when it has none."""
    return symbol.partition('.')[2][:1]


def tradable_securities(securities):
    return [s for s in securities if share_class(s['symbol']) in SHARE_TYPES]

def fetch_active_companies():
    """Fetch all active company symbols from CSE API."""
    url = 'https://www.cse.lk/api/allSecurityCode'
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    logging.info(f"Fetching active companies from {url}...")
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    
    data = r.json()
    logging.info(f"Found {len(data)} active securities.")
    return data

def fetch_company_info(symbol):
    """Fetch detailed metadata for a specific symbol."""
    url = 'https://www.cse.lk/api/companyInfoSummery'
    headers = {'User-Agent': 'Mozilla/5.0'}
    files = {'symbol': (None, symbol)}
    
    try:
        r = requests.post(url, files=files, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        data = r.json()
        return data.get('reqSymbolInfo', {})
    except Exception as e:
        logging.error(f"Failed to fetch info for {symbol}: {e}")
        return {}

def build_metadata():
    os.makedirs('data/processed', exist_ok=True)
    
    securities = fetch_active_companies()
    
    equities = tradable_securities(securities)
    logging.info(f"Filtered to {len(equities)} equity symbols.")
    
    records = []
    
    for i, sec in enumerate(equities):
        symbol = sec['symbol']
        logging.info(f"[{i+1}/{len(equities)}] Fetching metadata for {symbol}...")
        
        info = fetch_company_info(symbol)
        
        # Base ticker without suffix (e.g., COMB from COMB.N0000)
        base_ticker = symbol.split('.')[0]
        
        share_type = SHARE_TYPES[share_class(symbol)]
        
        # For Yahoo finance compatibility we used to append .CM, but it doesn't work.
        # Still, we keep the column per schema.
        yahoo_ticker = f"{base_ticker}.CM"
        
        records.append({
            'symbol': symbol,
            'company_name': info.get('name', sec['name']),
            'sector': 'Unknown', # Will be filled later if we find a mapping
            'board': 'Main', # Default, update if we find API
            'delisted': False,
            'delisting_date': None,
            'listing_date': info.get('issueDate'),
            'isin': info.get('isin'),
            'market_cap': info.get('marketCap'),
            'shares_outstanding': info.get('quantityIssued'),
            'par_value': info.get('parValue'),
            'base_ticker': base_ticker,
            'share_type': share_type,
            'yahoo_ticker': yahoo_ticker
        })
        
        time.sleep(REQUEST_SLEEP_SECONDS) # Polite rate limiting
        
    df = pd.DataFrame(records)
    
    output_path = 'data/processed/company_metadata.csv'
    df.to_csv(output_path, index=False)
    logging.info(f"Saved metadata to {output_path}")
    
    return df

if __name__ == '__main__':
    build_metadata()
