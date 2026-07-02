"""Operator tooling for the alias-vetting workflow (read-only exports + a
review-queue CLI). Nothing in here places bets or writes odds/picks data; the
ONLY database write in the package is review_queue_cli's mark command, which
updates match_review_queue.review_status/reviewed_at."""
