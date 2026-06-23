import requests
import pandas as pd
import time
import logging
import os
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

REQUEST_TIMEOUT_SECONDS = int(os.getenv("CSE_METADATA_TIMEOUT_SECONDS", "15"))
REQUEST_SLEEP_SECONDS = float(os.getenv("CSE_METADATA_SLEEP_SECONDS", "0.1"))

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
    
    # Keep listed security classes that can appear in the daily price snapshot.
    # Earlier recovery runs showed .U0000 units trading in tradeSummary; excluding
    # them makes the OHLCV metadata gate fail.
    tradable_suffixes = ('.N0000', '.X0000', '.U0000')
    equities = [s for s in securities if any(s['symbol'].endswith(suffix) for suffix in tradable_suffixes)]
    logging.info(f"Filtered to {len(equities)} equity symbols.")
    
    records = []
    
    for i, sec in enumerate(equities):
        symbol = sec['symbol']
        logging.info(f"[{i+1}/{len(equities)}] Fetching metadata for {symbol}...")
        
        info = fetch_company_info(symbol)
        
        # Base ticker without suffix (e.g., COMB from COMB.N0000)
        base_ticker = symbol.split('.')[0]
        
        # Determine voting status
        if '.X' in symbol:
            share_type = 'Non-Voting'
        elif '.U' in symbol:
            share_type = 'Unit'
        else:
            share_type = 'Voting'
        
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
