def test_spread_selections_carry_explicit_sign() -> None:
    """Audit 2026-07-10 (M136): settlement's _SIGNED_LINE_RE requires a signed
    line, so an unsigned positive spread ("Patriots 3.5") is permanently
    unsettleable. Spreads must format the point with an explicit sign; totals
    stay unsigned."""
    from app.ingestion.odds_api import OddsApiClient

    payload = [
        {
            "id": "ev1",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-07-10T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Patriots", "point": 3.5, "price": 1.95},
                                {"name": "Jets", "point": -3.5, "price": 1.95},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 45.5, "price": 1.9},
                                {"name": "Under", "point": 45.5, "price": 1.9},
                            ],
                        },
                    ],
                }
            ],
        }
    ]
    snaps = OddsApiClient._parse(OddsApiClient.__new__(OddsApiClient), payload)
    by_sel = {s.selection for s in snaps}
    assert "Patriots +3.5" in by_sel
    assert "Jets -3.5" in by_sel
    assert "Over 45.5" in by_sel  # totals unsigned, unchanged
