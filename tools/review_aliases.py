"""Turn a HUMAN-reviewed alias-candidate CSV into a seed patch + batch tests.

Accepts ONLY rows with ``human_decision=approve``. An approved row carrying ANY
risk flag REQUIRES a non-empty ``reviewer_notes`` (the human states why the flag
is safe) — else it is hard-rejected with a loud listing. Every surviving alias
passes the wrong-game guards (marker-crossing + canonical-collision refusals —
the CD-Nacional precedent).

Emits into --out-dir (default docs/review/):
  a) alias_patch_<date>.diff        — unified diff vs app/resolution/aliases_seed.json
  b) test_alias_batch_<date>.py     — regression-test skeleton (move to tests/
                                      AFTER applying the patch; --apply copies it)
  c) rejected_negative_suggestions_<date>.txt — commented _NOT_ADDED-style block

NEVER edits aliases_seed.json in place unless --apply is passed explicitly.
After --apply, the batch MUST pass:  uv run pytest -q  AND the wrong-game audit
(app/maintenance/wrong_game_audit.py — 0 new merges) before commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.resolution import AliasTable
from tools.alias_vetting import (
    apply_additions,
    build_alias_additions,
    load_review_csv,
    render_rejected_suggestions,
    render_seed,
    render_test_skeleton,
    split_decisions,
    unified_seed_diff,
)

_SEED_PATH = Path("app/resolution/aliases_seed.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reviewed alias CSV -> seed patch + batch tests")
    parser.add_argument("csv_path", type=Path, help="the reviewed alias_candidates CSV")
    parser.add_argument("--seed", type=Path, default=_SEED_PATH, help="aliases_seed.json path")
    parser.add_argument("--out-dir", type=Path, default=Path("docs/review"))
    parser.add_argument("--tests-dir", type=Path, default=Path("tests"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ALSO write the seed file + copy the batch test into tests/ "
        "(default: patch file only, seed untouched)",
    )
    args = parser.parse_args(argv)

    rows = load_review_csv(args.csv_path)
    split = split_decisions(rows)

    if split.rejected_missing_notes:
        print(
            "REJECTED — approved rows with risk_flags but EMPTY reviewer_notes "
            "(a flagged approval must say why the flag is safe):",
            file=sys.stderr,
        )
        for row in split.rejected_missing_notes:
            print(
                f"  {row.get('candidate_id', '?')}: {row.get('raw_name_a')!r} <-> "
                f"{row.get('raw_name_b')!r}  flags={row.get('risk_flags')}",
                file=sys.stderr,
            )

    seed_text = args.seed.read_text(encoding="utf-8")
    seed_data = json.loads(seed_text)
    seed_table = AliasTable.from_seed(args.seed)

    additions, errors = build_alias_additions(split.approved, seed_table)
    for err in errors:
        print(f"GUARD REFUSAL — {err}", file=sys.stderr)

    date_str = datetime.now(tz=UTC).date().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # (a) unified-diff patch — the ONLY default output touching seed content.
    new_data = apply_additions(seed_data, additions)
    new_text = render_seed(new_data)
    patch = unified_seed_diff(seed_text, new_text)
    patch_path = args.out_dir / f"alias_patch_{date_str}.diff"
    patch_path.write_text(patch, encoding="utf-8")

    # (b) regression-test skeleton for the batch (approved rows that survived
    # the guards — refused rows must not get a passing golden test).
    surviving_ids = {a.candidate_id for a in additions}
    surviving_rows = [r for r in split.approved if r.get("candidate_id") in surviving_ids]
    test_name = f"test_alias_batch_{date_str.replace('-', '_')}.py"
    test_path = args.out_dir / test_name
    test_path.write_text(render_test_skeleton(surviving_rows, date_str), encoding="utf-8")

    # (c) commented negative-alias suggestions from human-rejected rows.
    rejected_rows = [
        r for r in split.skipped if (r.get("human_decision") or "").strip().lower() == "reject"
    ]
    rejected_path = args.out_dir / f"rejected_negative_suggestions_{date_str}.txt"
    rejected_path.write_text(render_rejected_suggestions(rejected_rows), encoding="utf-8")

    print(
        f"rows={len(rows)} approved={len(split.approved)} "
        f"rejected_missing_notes={len(split.rejected_missing_notes)} "
        f"guard_refusals={len(errors)} additions={len(additions)}"
    )
    print(f"patch    -> {patch_path}")
    print(f"tests    -> {test_path}")
    print(f"rejected -> {rejected_path}")

    if args.apply:
        if not additions:
            print("--apply: no surviving additions — seed left untouched")
        else:
            args.seed.write_text(new_text, encoding="utf-8")
            applied_test = args.tests_dir / test_name
            applied_test.write_text(render_test_skeleton(surviving_rows, date_str), "utf-8")
            print(f"APPLIED  -> {args.seed} (+ {applied_test})")
            print(
                "NEXT: uv run pytest -q  AND run the wrong-game audit "
                "(app/maintenance/wrong_game_audit.py) — the batch ships only on "
                "0 new merges."
            )
    else:
        print("(dry run — seed NOT modified; pass --apply to write it)")

    return 1 if (split.rejected_missing_notes or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
