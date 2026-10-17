"""
seed_requests.py

One-off helper: puts the starting list of tickers into the requested_assets queue.
Safe to re-run; tickers that are already queued (or processed) are left alone.

Usage:
    python seed_requests.py
    python data_collection.py     # then process the queue
"""
import sys

from db import get_connection, queue_tickers

STOCKS = [
    'GOOG', 'GOOGL', 'MSFT', 'AAPL', 'BAC', 'SBUX', 'AMZN','META',
    'JNJ', 'LLY', 'UNH', 'XOM', 'JPM', 'CAT', 'HON', 'PG', 'COST', 'NEE','BRK-A','BRK-B'
]

FUNDS = [
    'VEIRX', 'TRBCX', 'DIA', 'QQQM', 'SWPPX', 'NCTWX', 'VTI', 'VEA', 'QQQ',
    'SCHB', 'SCHD', 'VOO', 'ONEQ', 'VBR', 'XMHQ', 'XMMO', 'RWK', 'XSVM',
    'FNDF', 'IEFA', 'FEZ', 'SPEM', 'VWO', 'DGS', 'EEMS', 'IPAC', 'IEUR',
    'FXAIX', 'FSMDX', 'FSKAX', 'IJR', 'VB', 'IWM', 'FSSNX', 'VBK', 'SLYG', 'IWO',
    'IJH', 'VO', 'VOT', 'IWS', 'SCZ', 'VSS', 'AVDV', 'EFG', 'EFV', 'VIGI',
    # Sector funds
    'XLV', 'VHT', 'XLE', 'VDE','XLU','XLI', 'XLP', 'XLB','VNQ',
]


def main():
    cnx = get_connection('RAILWAY_')
    cur = cnx.cursor()
    try:
        queued, existing, invalid = queue_tickers(cur, STOCKS + FUNDS)
        cnx.commit()
    except Exception as e:
        cnx.rollback()
        print(f'Failed to queue tickers: {e}')
        sys.exit(1)
    finally:
        cur.close()
        cnx.close()

    print(f'Newly queued: {queued}')
    print(f'Already in queue: {existing}')
    if invalid:
        print(f'Skipped invalid tickers: {invalid}')
        sys.exit(1)


if __name__ == '__main__':
    main()
