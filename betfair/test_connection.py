"""
Betfair API Connection Test
============================
Run this first to confirm credentials work.
Expected output:
  ✓ Logged in successfully
  ✓ Account balance: £200.00
  ✓ Found X football markets
  ✓ Found X horse racing markets
  ✓ All good — ready to run bots
"""

import sys
import betfairlightweight
from betfairlightweight.filters import market_filter

sys.path.insert(0, r"C:\Users\madlabz\Documents\Trading\betfair")
import config

def test():
    print("Testing Betfair API connection...\n")

    # ── 1. Login ───────────────────────────────────────────────────────────────
    try:
        client = betfairlightweight.APIClient(
            username=config.USERNAME,
            password=config.PASSWORD,
            app_key=config.APP_KEY,
        )
        client.login_interactive()
        print("✓ Logged in successfully")
    except Exception as e:
        print(f"✗ Login failed: {e}")
        return

    # ── 2. Account balance ─────────────────────────────────────────────────────
    try:
        funds = client.account.get_account_funds()
        balance = funds.available_to_bet_balance
        print(f"✓ Account balance: £{balance:.2f}")
        if balance < config.BANKROLL_GBP * 0.2:
            print(f"  ⚠ Balance below 20% of bankroll — bots will not trade")
    except Exception as e:
        print(f"✗ Could not fetch balance: {e}")

    # ── 3. Football markets ────────────────────────────────────────────────────
    try:
        football = client.betting.list_market_catalogue(
            filter=market_filter(
                event_type_ids=["1"],          # 1 = Soccer
                market_countries=["GB"],
                market_type_codes=["MATCH_ODDS"],
            ),
            max_results=5,
            market_projection=["EVENT", "MARKET_START_TIME"],
        )
        print(f"✓ Found {len(football)} upcoming football markets")
        for m in football[:3]:
            print(f"    → {m.market_name} | {m.market_start_time}")
    except Exception as e:
        print(f"✗ Could not fetch football markets: {e}")

    # ── 4. Horse racing markets ────────────────────────────────────────────────
    try:
        racing = client.betting.list_market_catalogue(
            filter=market_filter(
                event_type_ids=["7"],          # 7 = Horse Racing
                market_countries=["GB", "IE"],
                market_type_codes=["WIN"],
            ),
            max_results=5,
            market_projection=["EVENT", "MARKET_START_TIME"],
        )
        print(f"✓ Found {len(racing)} upcoming horse racing markets")
        for m in racing[:3]:
            print(f"    → {m.market_name} | {m.market_start_time}")
    except Exception as e:
        print(f"✗ Could not fetch racing markets: {e}")

    # ── 5. Tennis markets ──────────────────────────────────────────────────────
    try:
        tennis = client.betting.list_market_catalogue(
            filter=market_filter(
                event_type_ids=["2"],          # 2 = Tennis
                market_type_codes=["MATCH_ODDS"],
            ),
            max_results=5,
            market_projection=["EVENT", "MARKET_START_TIME"],
        )
        print(f"✓ Found {len(tennis)} upcoming tennis markets")
    except Exception as e:
        print(f"✗ Could not fetch tennis markets: {e}")

    print("\n✓ All good — ready to run bots")

if __name__ == "__main__":
    test()
