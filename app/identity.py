"""Provider-identity limits shared by ingestion, schemas, and persistence.

Limits are measured in UTF-8 bytes. PostgreSQL ``VARCHAR`` limits characters,
so using the same numeric widths in the ORM is conservative for every Unicode
identity accepted at the application boundary. Identities are never truncated:
truncation can alias two events, teams, books, or betting instruments.
"""

from typing import Final

SPORT_KEY_MAX_BYTES: Final = 128
SPORT_NAME_MAX_BYTES: Final = 256
LEAGUE_KEY_MAX_BYTES: Final = 256
COUNTRY_MAX_BYTES: Final = 128
TEAM_NAME_MAX_BYTES: Final = 256
EVENT_REF_MAX_BYTES: Final = 512
BOOKMAKER_MAX_BYTES: Final = 512
MARKET_DETAIL_MAX_BYTES: Final = 512
SELECTION_MAX_BYTES: Final = 1024


def require_bounded_identity(
    value: str,
    *,
    maximum_bytes: int,
    field: str,
    allow_empty: bool = False,
) -> str:
    """Return ``value`` when it is an admissible identity; never normalize it."""

    if not allow_empty and not value:
        raise ValueError(f"{field} must be non-empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} must be at most {maximum_bytes} UTF-8 bytes")
    return value
