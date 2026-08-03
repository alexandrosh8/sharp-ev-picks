"""GENERATED regression skeleton for the 2026-08-03 alias batch (review before
moving into tests/). Locks BOTH directions of the wrong-game-safety contract:
the vetted pair now strict-matches, and the alias NEVER crosses a
women/youth/reserve marker. Apply the seed patch FIRST, then run the
wrong-game audit (0 new merges) and
`uv run pytest tests/test_alias_batch_2026_08_03.py -q`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.resolution import EventCandidate, default_aliases, match_event, match_event_hardened

# (feed form, pinnacle/canonical form, real opponent from the observed fixture)
_BATCH: list[tuple[str, str, str]] = [
    ("EDM Elks", "Edmonton Elks", "saskatchewan roughriders"),
    ("HAM Tiger Cats", "Hamilton Tiger-Cats", "montreal alouettes"),
    ("Penrith P.", "Penrith Panthers", "albury wodonga bandits"),
    ("Atletico MG", "Atletico Mineiro", "palmeiras"),
    ("Botafogo RJ", "Botafogo FR RJ", "cruzeiro"),
    ("Gremio FBPA", "Gremio FBPA", "fluminense f c"),
    ("Cuiaba EC", "Cuiaba EC", "sport do recife"),
    ("Goias EC", "Goias EC", "crb"),
    ("Sao Bernardo SP", "Sao Bernardo SP", "ceara"),
    ("Audax RJ", "Audax Rio", "sao goncalo"),
    ("Tigres Brasil", "Tigres do Brasil", "rio barra"),
    ("Tre Fiori", "SP Tre Fiori", "larne"),
    ("CS Petrocub", "CS Petrocub", "egnatia rrogozhine"),
    ("NK Celje", "NK Celje", "egnatia rrogozhine"),
    ("SK Sturm Graz", "SK Sturm Graz", "hearts"),
    ("Vardar Skopje", "Vardar Skopje", "kups"),
    ("Cracovia", "Cracovia Krakow", "pafos"),
    ("Bray", "Bray Wanderers A.F.C.", "athlone"),
    ("Athlone", "Athlone Town F.C.", "bray"),
    ("Fram", "Fram Reykjavik", "keflavik"),
    ("Hviti", "Hviti Riddarinn", "dalvik reynir"),
    ("Hafnarfjordur", "FH Hafnarfjordur", "stjarnan"),
    ("Maardu", "Maardu Linnameeskond", "tallinna kalev"),
    ("Derry", "Derry City F.C.", "cska sofia"),
    ("PAOK Salonika", "PAOK Salonika", "dynamo kyiv"),
    ("Sheriff", "Sheriff Tiraspol", "aluminij"),
    ("Galway", "Galway United F.C.", "sligo rovers"),
    ("Guangzhou Dandelion", "Guangzhou Dandelion Alpha", "guangdong mingtu"),
    ("Nantong Haimen", "Nantong Haimen Codion", "dalian kewei"),
    ("Shanxi Chongde Ronghai", "Xi'an Chongde Ronghai", "shanghai segenda"),
    ("Wenzhou Professional", "Wenzhou Professional", "hangzhou linping wuyue"),
    ("Zhejiang Professional", "Zhejiang Professional", "shanghai shenhua"),
    ("Dep. Cuenca", "Deportivo Cuenca", "barcelona"),
    ("Villa Nova MG", "Villa Nova MG", "aymores"),
    ("Sydney Utd", "Sydney United", "blacktown city"),
    ("Launceston United", "Launceston United Soccer", "launceston city"),
    ("Hills United", "Hills United Brumbies", "northern tigers"),
    ("KFUM", "KFUM Oslo", "bodo glimt"),
    ("Sarpsborg", "Sarpsborg 08 FF", "viking"),
    ("SK Brann", "SK Brann", "start"),
    ("Drogheda", "Drogheda United", "bohemians"),
    ("Khangarid", "Khangarid Klub", "ulaanbaatar"),
    ("Zhenis", "Zhenis", "astana"),
    ("Brown Adrogue", "Brown de Adrogue", "deportivo armenio"),
    ("Talleres R.E.", "Talleres de Remedios", "argentino de quilmes"),
    ("Juventud Unida S.M.", "Juventud Unida San Miguel", "sacachispas"),
    ("Victoriano A.", "Victoriano Arenas", "deportivo paraguayo"),
    ("A. Guemes", "Guemes", "nueva chicago"),
    ("Central Norte", "Central Norte Salta", "san miguel"),
    ("Argentino MM", "Argentino Monte Maiz", "cipolletti"),
    ("Gimnasia E.R.", "Gimnasia y Esgrima de Concepcion", "9 de julio rafaela"),
    ("Abroath", "Arbroath", "spartans"),
    ("Airdrie", "Airdrieonians", "dundee"),
    ("Dundee Utd", "Dundee United", "stirling albion"),
    ("Hamilton", "Hamilton Academical", "peterhead"),
    ("Inverness", "Inverness CT", "east fife"),
    ("Queen of South", "Queen of the South", "kelty hearts"),
    ("Miramar", "Miramar Misiones", "atenas"),
    ("Floresta EC", "Floresta EC", "guarani"),
    ("Piracicaba", "XV de Piracicaba", "cianorte"),
    ("Treze PB", "Treze", "crac"),
    ("Dep. Santo Domingo", "Deportivo Santo Domingo", "independiente juniors"),
    ("Dinamo Tirana", "Dinamo City", "aluminij"),
    ("03 Differdange", "03 Differdange", "ilves"),
    ("CSKA 1948 Sofia", "CSKA 1948 Sofia", "spartak trnava"),
    ("Debrecen", "Debreceni VSC", "pyunik"),
    ("Hegelmann Litauen", "Hegelmann Litauen", "paide linnameeskond"),
    ("Katowice", "GKS Katowice", "zilina"),
    ("KF Dukagjini", "KF Dukagjini", "lugano"),
    ("La Fiorita", "SP La Fiorita", "una strassen"),
    ("Ludogorets", "Ludogorets Razgrad", "hapoel tel aviv"),
    ("Milsami Ursidos", "Milsami Ursidos", "velez mostar"),
    ("Polissya", "Polissya Zhytomyr", "copenhagen"),
    ("Rakow", "Rakow Czestochowa", "valletta"),
    ("Sileks", "Sileks Kratovo", "dinamo minsk"),
    ("Valur", "Valur Reykjavik", "zrinjski"),
    ("Zrinjski", "Zrinjski Mostar", "valur"),
    ("Zira IK", "Zira IK", "torpedo kutaisi"),
    ("Dziugas Telsiai", "Dziugas Telsiai", "kauno zalgiris"),
    ("Arsenal Dzerzhinsk", "Arsenal Dzyarzhynsk", "neman grodno"),
    ("Curtin Univ", "Curtin University", "cockburn city"),
    ("Inglewood Utd", "Inglewood United", "subiaco"),
    ("Murdoch Melville", "Murdoch University Melville", "floreat athena"),
    ("North Geelong", "North Geelong Warriors", "port melbourne sharks"),
    ("Port Melbourne Sharks", "Port Melbourne Sharks", "north geelong warriors"),
    ("Manningham United Blues", "Manningham United Blues", "north sunshine eagles"),
    ("Minnesota Utd", "Minnesota United", "san diego"),
    ("Nashville Soccer Club", "Nashville SC", "dc united"),
    ("DC United", "D.C. United", "nashville soccer"),
    ("Incheon Utd", "Incheon United", "anyang"),
    ("SER Caxias", "SER Caxias", "ferroviaria"),
    ("Ferroviaria", "Ferroviaria SP", "ser caxias"),
    ("Sport do Recife", "Sport Recife", "cuiaba ec"),
    ("KF Drita", "KF Drita", "kauno zalgiris"),
]

_KO = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_fixes_the_match(feed: str, pinnacle: str, opponent: str) -> None:
    aliases = default_aliases()
    assert aliases.canonical(feed) == aliases.canonical(pinnacle)
    cand = EventCandidate(ref="x", home=pinnacle, away=opponent, kickoff=_KO)
    assert match_event(feed, opponent, _KO, [cand], aliases=aliases) is cand


@pytest.mark.parametrize("marker", ["Women", "W", "U19", "U20", "II", "B", "Reserves"])
@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_never_crosses_a_marker(feed: str, pinnacle: str, opponent: str, marker: str) -> None:
    aliases = default_aliases()
    cand = EventCandidate(ref="x", home=f"{pinnacle} {marker}", away=opponent, kickoff=_KO)
    assert match_event_hardened(feed, opponent, _KO, [cand], aliases=aliases, ordered=True) is None


# --- 2026-08-03 corrective seed edits (reviewed alongside the batch) ----------
# Three POISON entries were removed and one split-brain group merged. These
# tests lock the corrections so a future import cannot silently reintroduce
# them (wrong-game class: a generic base claimed by one famous club).


def test_bare_atletico_no_longer_resolves_to_atletico_madrid() -> None:
    """Seed alias 'Atletico' (openfootball) made canonical('Atletico FC') —
    the Ecuadorian Serie B club — equal Atlético Madrid's base. Removed."""
    aliases = default_aliases()
    assert aliases.canonical("Atletico FC") == "atletico"
    assert aliases.canonical("Atletico") == "atletico"
    # the specific Madrid forms still resolve to Madrid
    assert aliases.canonical("Atl Madrid") == "atletico madrid"
    assert aliases.canonical("Atletico de Madrid") == "atletico madrid"


def test_bare_st_albans_no_longer_resolves_to_english_club() -> None:
    """Seed alias 'St. Albans' mapped the NPL-Victoria 'St Albans' (Saints)
    listing onto St Albans City FC (England). Bare form now unowned."""
    aliases = default_aliases()
    assert aliases.canonical("St Albans") == "st albans"
    assert aliases.canonical("St. Albans City FC") == "st albans city"
    assert aliases.canonical("St. Albans Saints") == "st albans saints"


def test_paysandu_bases_stay_unowned() -> None:
    """The Wikidata 'Paysandú F.C.' (Uruguay) entry claimed bases
    {'paysandu f c', 'paysandu'} that the Brazilian Paysandu SC's Pinnacle
    listing also uses — contested bases stay unowned (entry removed)."""
    aliases = default_aliases()
    assert aliases.canonical("Paysandu F.C.") == "paysandu f c"
    assert aliases.canonical("Paysandu") == "paysandu"
    assert aliases.canonical("Paysandu PA") == "paysandu pa"


def test_drogheda_split_brain_merged() -> None:
    """'Drogheda United' and 'Drogheda United F.C.' were two seed groups whose
    alias/key norms collided; merged into one group."""
    aliases = default_aliases()
    assert (
        aliases.canonical("Drogheda")
        == aliases.canonical("Drogheda United")
        == aliases.canonical("Drogheda United F.C.")
        == "drogheda united"
    )


@pytest.mark.parametrize(
    ("feed", "arcadia"),
    [
        ("Derry", "Derry City F.C."),
        ("Galway", "Galway United F.C."),
        ("Bray", "Bray Wanderers A.F.C."),
        ("Athlone", "Athlone Town F.C."),
        ("Sarpsborg", "Sarpsborg 08"),
        ("Gremio FBPA", "Gremio"),
        ("Goias EC", "Goias"),
        ("KF Drita", "Drita"),
        ("Dinamo Tirana", "Dinamo City"),
        ("HAM Tiger Cats", "Hamilton Tiger-Cats"),
        ("Nashville Soccer Club", "Nashville"),
        ("DC United", "D.C. United"),
    ],
)
def test_batch_bside_forms_actually_group(feed: str, arcadia: str) -> None:
    """The generated skeleton tests feed-vs-KEY; these lock feed-vs-OBSERVED
    arcadia form (incl. dotted F.C. variants appended into existing groups)."""
    aliases = default_aliases()
    assert aliases.canonical(feed) == aliases.canonical(arcadia)
