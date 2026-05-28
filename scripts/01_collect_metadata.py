import requests
import pandas as pd
import time
import logging
import os
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def fetch_active_companies():
    """Fetch all active company symbols from CSE API."""
    url = 'https://www.cse.lk/api/allSecurityCode'
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    logging.info(f"Fetching active companies from {url}...")
    r = requests.get(url, headers=headers)
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
        r = requests.post(url, files=files, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get('reqSymbolInfo', {})
    except Exception as e:
        logging.error(f"Failed to fetch info for {symbol}: {e}")
        return {}

def build_metadata():
    os.makedirs('data/processed', exist_ok=True)
    
    securities = fetch_active_companies()
    
    # Filter to only equity symbols (usually end in .N0000 or .X0000)
    # The API returns things like ABC.N0000 (voting), ABC.X0000 (non-voting)
    equities = [s for s in securities if '.N0000' in s['symbol'] or '.X0000' in s['symbol']]
    logging.info(f"Filtered to {len(equities)} equity symbols.")
    
    records = []
    
    for i, sec in enumerate(equities):
        symbol = sec['symbol']
        logging.info(f"[{i+1}/{len(equities)}] Fetching metadata for {symbol}...")
        
        info = fetch_company_info(symbol)
        
        # Base ticker without suffix (e.g., COMB from COMB.N0000)
        base_ticker = symbol.split('.')[0]
        
        # Determine voting status
        share_type = 'Non-Voting' if '.X' in symbol else 'Voting'
        
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
        
        time.sleep(0.5) # Polite rate limiting
        
    df = pd.DataFrame(records)
    
    output_path = 'data/processed/company_metadata.csv'
    df.to_csv(output_path, index=False)
    logging.info(f"Saved metadata to {output_path}")
    
    return df

if __name__ == '__main__':
    build_metadata()
