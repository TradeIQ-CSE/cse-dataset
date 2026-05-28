import requests
import pandas as pd
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def get_symbols_for_date(date_str):
    url = 'https://www.cse.lk/api/tradeSummary'
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r = requests.post(url, files={'date': (None, date_str)}, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if 'reqTradeSummery' in data:
                return data['reqTradeSummery']
    except Exception as e:
        pass
    return []

def find_delisted():
    meta = pd.read_csv('data/processed/company_metadata.csv')
    active_symbols = set(meta['symbol'])
    
    # Check end of year dates across history to find past companies
    dates_to_check = [f"{year}-12-28" for year in range(2010, 2026)]
    
    all_historic = {}
    for date_str in dates_to_check:
        logging.info(f"Checking historic equities for {date_str}...")
        equities = get_symbols_for_date(date_str)
        for eq in equities:
            symbol = eq.get('symbol')
            if symbol and symbol not in active_symbols:
                if symbol not in all_historic:
                    all_historic[symbol] = {
                        'symbol': symbol,
                        'company_name': eq.get('name'),
                        'sector': 'Unknown',
                        'board': 'Delisted',
                        'delisted': True,
                        'delisting_date': None,
                        'listing_date': eq.get('issueDate'),
                        'isin': None,
                        'market_cap': None,
                        'shares_outstanding': None,
                        'par_value': None,
                        'base_ticker': symbol.split('.')[0],
                        'share_type': 'Non-Voting' if '.X' in symbol else 'Voting',
                        'yahoo_ticker': f"{symbol.split('.')[0]}.CM"
                    }
        time.sleep(0.5)
        
    logging.info(f"Found {len(all_historic)} delisted companies.")
    
    # Append to metadata
    if len(all_historic) > 0:
        delisted_df = pd.DataFrame(list(all_historic.values()))
        meta = pd.concat([meta, delisted_df], ignore_index=True)
        meta.to_csv('data/processed/company_metadata.csv', index=False)
        logging.info("Saved updated metadata.")

if __name__ == '__main__':
    find_delisted()
