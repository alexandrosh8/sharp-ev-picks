#!/bin/bash
# Safety audit (ADR-0002): proves no bet-placement code path exists.
# Runs locally and in CI; ANY finding fails the build.

set -u
cd "$(dirname "$0")/.." || exit 1
fail=0

echo "== 1. order-placement + account identifiers must be ABSENT from app/ + scripts/ =="
# Betfair read-only market-data methods (listEventTypes/listCompetitions/
# listEvents/listMarketCatalogue/listMarketBook) are operator-authorized for the
# strictly read-only price feed (CLAUDE.md Rule 1 read-only exception, commit
# 0e27433, 2026-06-29). They return prices only and place nothing, so they are NOT
# banned. What IS banned is every order-PLACEMENT and account/order-LEDGER method:
# placeOrders/cancelOrders/replaceOrders/updateOrders write bets;
# listCurrentOrders/listClearedOrders read a betting account. None may ever appear.
# Case-insensitive, and scripts/ is scanned too (this file self-excluded — it must
# name the banned identifiers to grep for them).
if grep -I --exclude-dir=__pycache__ -rniE --exclude=safety_audit.sh "placeOrder|place_order|placeBets|place_bet|cancelOrder|cancel_order|replaceOrders|updateOrders|listCurrentOrders|listClearedOrders" app/ scripts/; then
  echo "FAIL: order-placement / account-order identifiers found in app/ or scripts/"
  fail=1
fi

echo "== 2. browser/login automation must be ABSENT from app/ =="
if grep -I --exclude-dir=__pycache__ -rnE "import selenium|from selenium|import playwright|from playwright" app/; then
  echo "FAIL: browser automation imports found in app/"
  fail=1
fi

echo "== 3. exchange execution libraries must never be dependencies =="
# betfairlightweight/flumine/betconnect/betdaq all ship order placement. The
# lockfile is scanned too so a transitive dependency cannot smuggle one in.
# \b guards flumine against the club name "Fluminense" in aliases_seed.json.
if grep -I --exclude-dir=__pycache__ -rniE "betfairlightweight|\bflumine\b|\bbetconnect\b|\bbetdaq\b" app/ pyproject.toml uv.lock; then
  echo "FAIL: exchange-execution library reference found (ships bet execution — ADR-0011)"
  fail=1
fi

echo "== 4. credential-storage patterns must be ABSENT from app/ =="
if grep -I --exclude-dir=__pycache__ -rnE "(bookmaker|betfair|betting)_(password|cookie|session_token)" app/; then
  echo "FAIL: betting-credential storage patterns found"
  fail=1
fi
# The REAL betfair credential field names in use may exist ONLY in app/config.py
# (Settings/SecretStr — env read at the composition root, never elsewhere).
# betfair_api_proxy\b deliberately skips the sanctioned Settings accessor
# betfair_api_proxy_url() in the scheduler.
if grep -I --exclude-dir=__pycache__ -rnE --exclude=config.py "betfair_app_key|betfair_read_only_username|betfair_read_only_password|betfair_api_proxy\b" app/; then
  echo "FAIL: betfair credential field names outside app/config.py"
  fail=1
fi

echo "== 5. suspended providers must be ABSENT =="
if grep -I --exclude-dir=__pycache__ -rniE "api[-_]?football" app/ pyproject.toml; then
  echo "FAIL: API-Football reference found (SUSPENDED provider)"
  fail=1
fi

echo "== 6. safety validator must be PRESENT in app/config.py =="
if ! grep -q "SAFETY VIOLATION" app/config.py; then
  echo "FAIL: picks-only validator missing from app/config.py"
  fail=1
fi
# The actual validator FUNCTION must exist — a comment containing the message
# string alone cannot satisfy this check.
if ! grep -q "def _enforce_picks_only" app/config.py; then
  echo "FAIL: _enforce_picks_only model_validator missing from app/config.py"
  fail=1
fi

echo "== 7. safety defaults must be PRESENT in .env.example =="
for needle in "PICKS_ONLY=true" "MANUAL_BETTING_ONLY=true" "AUTO_BETTING=false" "BET_EXECUTION_ENABLED=false" "READ_ONLY_MARKET_DATA=true"; do
  if ! grep -q "$needle" .env.example; then
    echo "FAIL: $needle missing from .env.example"
    fail=1
  fi
done

echo "== 8. alerts must carry the manual-betting reminder =="
if ! grep -rq "This system does not place bets" app/schemas/picks.py; then
  echo "FAIL: manual-betting reminder constant missing"
  fail=1
fi

echo "== 9. betfair_api.py must enforce the runtime read-only op allowlist =="
# The JSON-RPC endpoint also serves order-placement ops, so the client must
# refuse any op outside the read-only allowlist BEFORE any login or HTTP.
if ! grep -q "_ALLOWED_OPS = frozenset" app/ingestion/betfair_api.py; then
  echo "FAIL: _ALLOWED_OPS frozenset missing from app/ingestion/betfair_api.py"
  fail=1
fi
if ! grep -q "not in _ALLOWED_OPS" app/ingestion/betfair_api.py; then
  echo "FAIL: not-in-allowlist refusal missing from app/ingestion/betfair_api.py"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "SAFETY AUDIT: FAILED"
  exit 1
fi
echo "SAFETY AUDIT: PASSED — no bet-placement code path exists"
exit 0
