import json
from services.track import backfill_all_watchlist_returns

if __name__ == "__main__":
    r = backfill_all_watchlist_returns()
    print("BACKFILL_RESULT=" + json.dumps(r, ensure_ascii=False))
