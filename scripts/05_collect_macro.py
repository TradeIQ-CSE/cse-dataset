import os
import logging
import pandas as pd
import yfinance as yf
from datetime import datetime
 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
 
def setup():
    os.makedirs('data/processed/macro', exist_ok=True)
    os.makedirs('data/raw/macro', exist_ok=True)
 
def _ticker_history(ticker_symbol, start="2000-01-01"):
    try:
        t = yf.Ticker(ticker_symbol)
        df = t.history(start=start, auto_adjust=True)
        if not df.empty:
            return df
    except Exception as e:
        logging.warning(f"yf.Ticker({ticker_symbol}) failed: {e}")
    return pd.DataFrame()


def fetch_usd_lkr_wb():
    try:
        import wbdata
        df = wbdata.get_dataframe({"PA.NUS.FCRF": "usd_lkr"}, country="LKA")
        df = df.reset_index().rename(columns={"date": "year"})
        df["date"] = pd.to_datetime(df["year"], format="%Y") + pd.offsets.YearEnd(0)
        df = df[["date", "usd_lkr"]].dropna()
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        return df
    except Exception as e:
        logging.warning(f"World Bank USD/LKR fallback failed: {e}")
        return pd.DataFrame()


def fetch_yfinance_data():
    logging.info("Fetching USD/LKR...")
    lkr_tickers = ["USRLKR=X", "LKR=X", "USDLKR=X"]
    lkr_df = pd.DataFrame()
    for ticker in lkr_tickers:
        df = _ticker_history(ticker)
        if not df.empty:
            lkr_df = df[["Close"]].reset_index().rename(columns={"Date": "date", "Close": "usd_lkr", "Datetime": "date"})
            lkr_df["date"] = pd.to_datetime(lkr_df["date"]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
            if "usd_lkr" not in lkr_df.columns:
                lkr_df.columns = ["date", "usd_lkr"]
            logging.info(f"Got USD/LKR from {ticker}: {len(lkr_df)} rows")
            break

    if lkr_df.empty:
        logging.warning("yfinance USD/LKR failed, using World Bank annual fallback...")
        lkr_df = fetch_usd_lkr_wb()

    if not lkr_df.empty:
        lkr_df.to_csv("data/processed/macro/usd_lkr_daily.csv", index=False)
        logging.info(f"Saved usd_lkr_daily.csv with {len(lkr_df)} rows.")
    else:
        logging.error("Could not fetch USD/LKR from any source.")

    logging.info("Fetching Global Indices from stooq.com...")
    import requests as _requests
    from io import StringIO
    stooq_map = {"^spx": "sp500", "^nkx": "nikkei225", "^hsi": "hangseng"}
    frames = {}
    import time as _time
    for sym, col in stooq_map.items():
        _time.sleep(2)
        try:
            url = f"https://stooq.com/q/d/l/?s={sym}&d1=20000101&d2=20260310&i=d"
            r = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            df = pd.read_csv(StringIO(r.text))
            if "Date" in df.columns and "Close" in df.columns:
                df = df[["Date", "Close"]].rename(columns={"Date": "date", "Close": col})
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                frames[col] = df.set_index("date")
                logging.info(f"Got {sym} ({col}): {len(df)} rows from stooq")
            else:
                logging.warning(f"Unexpected stooq response for {sym}: columns={df.columns.tolist()}")
        except Exception as e:
            logging.warning(f"stooq fetch failed for {sym}: {e}")

    if frames:
        close_idx = pd.concat(frames.values(), axis=1).reset_index().rename(columns={"index": "date"})
        close_idx.to_csv("data/processed/macro/global_indices.csv", index=False)
        logging.info(f"Saved global_indices.csv with {len(close_idx)} rows.")
    else:
        logging.error("Could not fetch any global index data.")
 
def fetch_wbdata():
    logging.info("Fetching Macro Data from World Bank...")
    try:
        import wbdata
        indicators = {
            "NY.GDP.MKTP.KD.ZG": "gdp_growth_pct",
            "FP.CPI.TOTL.ZG": "inflation_pct",
            "FR.INR.DPST": "deposit_rate_pct"
        }
        df = wbdata.get_dataframe(indicators, country="LKA")
        df = df.reset_index()
        df = df.rename(columns={'date': 'year'})
        df['date'] = pd.to_datetime(df['year'], format='%Y') + pd.offsets.YearEnd(0)
        df['date'] = df['date'].dt.strftime('%Y-%m-%d')
        df.to_csv('data/processed/macro/cbsl_indicators.csv', index=False)
        logging.info(f"Saved cbsl_indicators.csv with {len(df)} rows.")
    except Exception as e:
        logging.error(f"Could not fetch wbdata: {e}")
 
def normalize_interest_rates():
    output = 'data/processed/macro/interest_rates.csv'
    candidates = [
        'data/raw/macro/interest_rates.csv',
        'data/raw/macro/interest_rates.xlsx',
        'data/raw/macro/cbsl_interest_rates.csv',
        'data/raw/macro/cbsl_interest_rates.xlsx',
    ]
    source_path = next((p for p in candidates if os.path.exists(p)), None)
    columns = ['date', 'tbill_3m', 'tbill_6m', 'tbill_12m', 'policy_rate', 'source']

    if source_path is None:
        pd.DataFrame(columns=columns).to_csv(output, index=False)
        logging.info("Created empty interest_rates.csv; place CBSL export in data/raw/macro/ to populate it.")
        return

    if source_path.endswith('.xlsx'):
        raw = pd.read_excel(source_path)
    else:
        raw = pd.read_csv(source_path)

    def find_col(aliases):
        normalized = {str(c).strip().lower().replace(' ', '_').replace('-', '_'): c for c in raw.columns}
        for alias in aliases:
            key = alias.lower().replace(' ', '_').replace('-', '_')
            if key in normalized:
                return normalized[key]
        return None

    mapping = {
        'date': find_col(['date', 'period', 'week_ending', 'month']),
        'tbill_3m': find_col(['tbill_3m', 't_bill_3m', '3_month_t_bill', '91_day_t_bill', '91_day_treasury_bill_rate', '91_days']),
        'tbill_6m': find_col(['tbill_6m', 't_bill_6m', '6_month_t_bill', '182_day_t_bill', '182_day_treasury_bill_rate', '182_days']),
        'tbill_12m': find_col(['tbill_12m', 't_bill_12m', '12_month_t_bill', '364_day_t_bill', '364_day_treasury_bill_rate', '364_days']),
        'policy_rate': find_col(['policy_rate', 'standing_lending_facility_rate', 'slfr', 'standing_deposit_facility_rate', 'sdf_rate', 'repo_rate']),
    }
    if mapping['date'] is None:
        raise ValueError(f"Interest-rate source {source_path} must include a date column")

    out = pd.DataFrame()
    out['date'] = pd.to_datetime(raw[mapping['date']], errors='coerce')
    for col in ['tbill_3m', 'tbill_6m', 'tbill_12m', 'policy_rate']:
        src = mapping[col]
        out[col] = pd.to_numeric(raw[src], errors='coerce') if src else pd.NA
    out['source'] = os.path.basename(source_path)
    out = out.dropna(subset=['date']).sort_values('date').drop_duplicates('date')
    out['date'] = out['date'].dt.strftime('%Y-%m-%d')
    out[columns].to_csv(output, index=False)
    logging.info("Saved normalized interest rates from %s with %d rows.", source_path, len(out))


def create_placeholders():
    path = 'data/processed/macro/interest_rates.csv'
    if not os.path.exists(path):
        pd.DataFrame(columns=['date', 'tbill_3m', 'tbill_6m', 'tbill_12m', 'policy_rate', 'source']).to_csv(path, index=False)
        logging.info("Created placeholder for interest_rates.csv (Requires manual CBSL download)")
 
def main():
    setup()
    fetch_yfinance_data()
    fetch_wbdata()
    normalize_interest_rates()
 
if __name__ == "__main__":
    main()
