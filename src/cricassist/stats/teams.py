"""Team names: franchises that were renamed, and common short names.

A renamed franchise is treated as one team under its current name, so a
head-to-head or venue record covers its whole history. Only clear renames of
the same franchise are listed. BPL and LPL teams are left as Cricsheet names
them, because their franchises changed owners and cities too often to say
which ones are the same team.
"""

# Current name: earlier names of the same franchise.
RENAMED = {
    # IPL (and WPL for Royal Challengers)
    "Delhi Capitals": ["Delhi Daredevils"],
    "Punjab Kings": ["Kings XI Punjab"],
    "Royal Challengers Bengaluru": ["Royal Challengers Bangalore"],
    "Rising Pune Supergiant": ["Rising Pune Supergiants"],
    # The Hundred (renamed for 2026)
    "MI London": ["Oval Invincibles"],
    "Sunrisers Leeds": ["Northern Superchargers"],
    "Manchester Super Giants": ["Manchester Originals"],
    # CPL
    "Trinbago Knight Riders": ["Trinidad & Tobago Red Steel"],
    "St Lucia Kings": ["St Lucia Zouks", "St Lucia Stars"],
    "Barbados Royals": ["Barbados Tridents"],
    # ILT20
    "Sharjah Warriorz": ["Sharjah Warriors"],
}

# Short names journalists use: name to current team name.
SHORT_NAMES = {
    "CSK": "Chennai Super Kings",
    "MI": "Mumbai Indians",
    "RCB": "Royal Challengers Bengaluru",
    "KKR": "Kolkata Knight Riders",
    "SRH": "Sunrisers Hyderabad",
    "DC": "Delhi Capitals",
    "PBKS": "Punjab Kings",
    "RR": "Rajasthan Royals",
    "LSG": "Lucknow Super Giants",
    "GT": "Gujarat Titans",
    "TKR": "Trinbago Knight Riders",
    "UAE": "United Arab Emirates",
    "USA": "United States of America",
    "WI": "West Indies",
    "SA": "South Africa",
    "NZ": "New Zealand",
    "SL": "Sri Lanka",
    "Aus": "Australia",
    "Eng": "England",
    "Ind": "India",
    "Pak": "Pakistan",
    "Ban": "Bangladesh",
    "Afg": "Afghanistan",
}


def alias_rows() -> list[tuple[str, str]]:
    """(name, team) pairs for every current, earlier and short name."""
    rows = [(team, team) for team in RENAMED]
    rows += [(old, team) for team, olds in RENAMED.items() for old in olds]
    rows += list(SHORT_NAMES.items())
    return rows
