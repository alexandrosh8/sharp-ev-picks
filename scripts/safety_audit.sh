#!/bin/bash
# Safety audit (ADR-0002): proves no bet-placement code path exists.
# Runs locally and in CI; ANY finding fails the build.

set -u
cd "$(dirname "$0")/.." || exit 1
fail=0

echo "== 1. order-placement identifiers must be ABSENT from app/ =="
if grep -rnE "placeOrder|place_order|placeBets|place_bet|cancelOrder|cancel_order|listMarketBook|replaceOrders" app/; then
  echo "FAIL: order-placement identifiers found in app/"
  fail=1
fi

echo "== 2. browser/login automation must be ABSENT from app/ =="
if grep -rnE "import selenium|from selenium|import playwright|from playwright" app/; then
  echo "FAIL: browser automation imports found in app/"
  fail=1
fi

echo "== 3. exchange execution libraries must never be dependencies =="
if grep -rni "betfairlightweight" app/ pyproject.toml; then
  echo "FAIL: betfairlightweight reference found (ships bet execution — ADR-0011)"
  fail=1
fi

echo "== 4. credential-storage patterns must be ABSENT from app/ =="
if grep -rnE "(bookmaker|betfair|betting)_(password|cookie|session_token)" app/; then
  echo "FAIL: betting-credential storage patterns found"
  fail=1
fi

echo "== 5. suspended providers must be ABSENT =="
if grep -rniE "api[-_]?football" app/ pyproject.toml; then
  echo "FAIL: API-Football reference found (SUSPENDED provider)"
  fail=1
fi

echo "== 6. safety validator must be PRESENT in app/config.py =="
if ! grep -q "SAFETY VIOLATION" app/config.py; then
  echo "FAIL: picks-only validator missing from app/config.py"
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

if [ "$fail" -ne 0 ]; then
  echo "SAFETY AUDIT: FAILED"
  exit 1
fi
echo "SAFETY AUDIT: PASSED — no bet-placement code path exists"
exit 0
