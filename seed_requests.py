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

STOCKS = ['GOOG', 'MSFT', 'AAPL', 'BAC', 'SBUX', 'AMZN']
FUNDS = ['VEIRX', 'TRBCX', 'DIA', 'QQQM', 'SWPPX', 'NCTWX', 'VTI', 'VEA',
         'SCHB', 'SCHD', 'VOO', 'ONEQ', 'VBR', 'XMHQ', 'XMMO', 'RWK', 'XSVM',
         'FNDF', 'IEFA', 'FEZ', 'SPEM', 'VWO', 'DGS', 'EEMS', 'IPAC', 'IEUR',
         'FXAIX', 'FSMDX', 'FSKAX']


def main():
    cnx = get_connection()
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
