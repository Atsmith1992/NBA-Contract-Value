"""
clean_data.py
-------------
Step 1 of the NBA Contract Value Finder pipeline.

Reads the 5 raw Basketball-Reference exports in data/raw/, fixes their quirks,
and writes clean, analysis-ready CSVs to data/clean/.

Raw-data quirks this script handles:
  1. Junk lines above the header (credit line, blank lines, the extra
     "Salary" header row in contracts)
  2. Header rows repeated in the middle of a table (checked just in case)
  3. The "League Average" summary row at the bottom of each stats table
  4. Traded players: one combined row (2TM / 3TM / 4TM) plus one row per
     team -> keep only the combined row so each player counts once
  5. Names with accents and suffixes (Jokic, Jr., III) -> we match players
     on Basketball-Reference's player ID instead of their name
  6. Salary text like "$62587158" -> a real number
  7. Contracts: the same player listed twice (old team still paying a
     waived contract) -> keep one row per player and log the duplicates

Outputs (in data/clean/):
  advanced_3yr.csv     one row per player per season (2023-24 to 2025-26)
  per_game_2026.csv    one row per player, 2025-26 per-game stats
  contracts.csv        one row per player, 2026-27 salary and contract info
  cleaning_log.txt     what was removed and why, plus data checks

Uses only the Python standard library.
Run it from the repo folder:   python clean_data.py
"""

import csv
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Paths are built from where this script lives, so it works no matter which
# folder the terminal is in.
BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "data" / "raw"
CLEAN_DIR = BASE_DIR / "data" / "clean"

# Season label -> raw file name.
# Basketball-Reference names a season by the year it ENDS (2023-24 = 2024).
ADVANCED_FILES = {
    "2023-24": "advanced_2024.csv",
    "2024-25": "advanced_2025.csv",
    "2025-26": "advanced_2026.csv",
}
PER_GAME_FILE = "per_game_2026.csv"
CONTRACTS_FILE = "contracts.csv"

# A traded player's combined row has a team code like 2TM, 3TM, 4TM.
MULTI_TEAM = re.compile(r"^\d+TM$")

# A contract season column looks like "2026-27".
SEASON_COLUMN = re.compile(r"^\d{4}-\d{2}$")

# Columns to keep from each file: (raw column name, clean column name, type)
# Types: "text", "int", "num" (decimal number)
ADVANCED_COLUMNS = [
    ("Player", "player", "text"),
    ("Age", "age", "int"),
    ("Team", "team", "text"),
    ("Pos", "pos", "text"),
    ("G", "games", "int"),
    ("GS", "games_started", "int"),
    ("MP", "minutes", "int"),
    ("PER", "per", "num"),
    ("TS%", "ts_pct", "num"),
    ("USG%", "usg_pct", "num"),
    ("OWS", "ows", "num"),
    ("DWS", "dws", "num"),
    ("WS", "ws", "num"),
    ("WS/48", "ws_per_48", "num"),
    ("OBPM", "obpm", "num"),
    ("DBPM", "dbpm", "num"),
    ("BPM", "bpm", "num"),
    ("VORP", "vorp", "num"),
]

PER_GAME_COLUMNS = [
    ("Player", "player", "text"),
    ("Age", "age", "int"),
    ("Team", "team", "text"),
    ("Pos", "pos", "text"),
    ("G", "games", "int"),
    ("GS", "games_started", "int"),
    ("MP", "mpg", "num"),
    ("PTS", "ppg", "num"),
    ("TRB", "rpg", "num"),
    ("AST", "apg", "num"),
    ("STL", "spg", "num"),
    ("BLK", "bpg", "num"),
    ("TOV", "tov_pg", "num"),
    ("FG%", "fg_pct", "num"),
    ("3P%", "fg3_pct", "num"),
    ("FT%", "ft_pct", "num"),
]

# Everything printed is also saved to cleaning_log.txt
log_lines = []


def log(message=""):
    print(message)
    log_lines.append(message)


# ---------------------------------------------------------------------------
# Small helpers to turn text into numbers
# ---------------------------------------------------------------------------

def to_number(text):
    """'.584' -> 0.584, '-1.2' -> -1.2, '' -> None (blank stays blank)."""
    text = (text or "").strip()
    if text == "":
        return None
    return float(text)


def to_int(text):
    """'79' -> 79, '' -> None."""
    number = to_number(text)
    return None if number is None else int(number)


def to_money(text):
    """'$62587158' or '$62,587,158' -> 62587158, '' -> None."""
    text = (text or "").strip().replace("$", "").replace(",", "")
    return int(text) if text else None


CONVERTERS = {"text": lambda t: (t or "").strip(), "int": to_int, "num": to_number}


def pick_columns(row, column_spec):
    """Keep only the columns we want, renamed and converted to the right type."""
    clean = {"player_id": row["player_id"]}
    for raw_name, clean_name, kind in column_spec:
        clean[clean_name] = CONVERTERS[kind](row.get(raw_name))
    return clean


# ---------------------------------------------------------------------------
# Reading a raw Basketball-Reference export
# ---------------------------------------------------------------------------

def read_raw_table(filename):
    """
    Read one raw export and return its data rows as dictionaries.

    Skips everything above the real header (the first line starting with
    'Rk,'), repeated header rows, blank lines, and the League Average row.
    """
    path = RAW_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Save it into data/raw first.")

    # utf-8-sig quietly removes an invisible marker some editors add to the
    # start of a file. utf-8 keeps accented names (Jokic, Doncic) intact.
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = f.read().splitlines()

    # Find the real header line.
    header_index = next(
        (i for i, line in enumerate(lines) if line.startswith("Rk,")), None
    )
    if header_index is None:
        raise ValueError(f"{filename}: no header line starting with 'Rk,' found")

    reader = csv.reader(lines[header_index:])
    header = next(reader)

    # The player ID is always the last column, but its name differs by file
    # ("Player-additional" in stats files, "-9999" in contracts). Standardize it.
    header[-1] = "player_id"

    counts = {"repeated_headers": 0, "blank_lines": 0, "league_average": 0}
    rows = []
    for values in reader:
        if not any(v.strip() for v in values):
            counts["blank_lines"] += 1
            continue
        if values[0] == "Rk":
            counts["repeated_headers"] += 1
            continue
        row = dict(zip(header, values))
        if row.get("Player") == "League Average" or row.get("player_id") == "-9999":
            counts["league_average"] += 1
            continue
        if not row.get("player_id"):
            raise ValueError(f"{filename}: a row has no player ID: {values[:3]}")
        rows.append(row)

    log(f"  {filename}: skipped {header_index} line(s) above the header, "
        f"{counts['repeated_headers']} repeated header(s), "
        f"{counts['league_average']} League Average row(s) -> {len(rows)} data rows")
    return rows


# ---------------------------------------------------------------------------
# Traded players: keep the combined row
# ---------------------------------------------------------------------------

def collapse_traded_players(rows, filename):
    """
    A traded player appears as one combined row (team = 2TM/3TM/4TM) followed
    by one row per team. Keep only the combined row so totals aren't counted
    twice. Players who stayed on one team have just one row; keep it as is.
    """
    groups = {}  # player_id -> list of rows (dicts keep insertion order)
    for row in rows:
        groups.setdefault(row["player_id"], []).append(row)

    kept = []
    dropped = 0
    for player_id, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        combined = [r for r in group if MULTI_TEAM.match(r["Team"])]
        if len(combined) != 1:
            raise ValueError(
                f"{filename}: {player_id} has {len(group)} rows "
                f"but not exactly one combined (TM) row"
            )
        kept.append(combined[0])
        dropped += len(group) - 1

    traded = sum(1 for g in groups.values() if len(g) > 1)
    log(f"  {filename}: {traded} traded players -> removed {dropped} "
        f"single-team rows -> {len(kept)} players")
    return kept


# ---------------------------------------------------------------------------
# Cleaning each file type
# ---------------------------------------------------------------------------

def clean_advanced():
    """Combine the three seasons of advanced stats into one long table."""
    all_rows = []
    for season, filename in ADVANCED_FILES.items():
        rows = collapse_traded_players(read_raw_table(filename), filename)
        for row in rows:
            clean = pick_columns(row, ADVANCED_COLUMNS)
            clean = {"season": season, **clean}  # put season first
            all_rows.append(clean)
    return all_rows


def clean_per_game():
    rows = collapse_traded_players(read_raw_table(PER_GAME_FILE), PER_GAME_FILE)
    return [{"season": "2025-26", **pick_columns(row, PER_GAME_COLUMNS)} for row in rows]


def clean_contracts():
    """
    One row per player with their 2026-27 salary.

    Some players are listed twice (an old team still owes part of a waived
    contract). The salary columns match on both rows, so we keep the row
    with the larger guaranteed amount and log the duplicate.
    """
    rows = read_raw_table(CONTRACTS_FILE)
    season_cols = [c for c in rows[0].keys() if SEASON_COLUMN.match(c)]
    if not season_cols or season_cols[0] != "2026-27":
        raise ValueError(f"Expected 2026-27 as the first salary column, got {season_cols}")

    by_player = {}
    duplicates = []
    for row in rows:
        salaries = [to_money(row[c]) for c in season_cols]
        listed = [s for s in salaries if s is not None]
        clean = {
            "player_id": row["player_id"],
            "player": row["Player"].strip(),
            "contract_team": row["Tm"].strip(),
            "salary_2026_27": salaries[0],
            "seasons_listed": len(listed),       # includes option years
            "total_listed": sum(listed),
            "guaranteed": to_money(row["Guaranteed"]),
        }

        player_id = clean["player_id"]
        if player_id not in by_player:
            by_player[player_id] = clean
            continue

        # Duplicate: decide which row to keep
        first = by_player[player_id]
        same_salary = first["salary_2026_27"] == clean["salary_2026_27"]
        keep = clean if (clean["guaranteed"] or 0) > (first["guaranteed"] or 0) else first
        drop = first if keep is clean else clean
        by_player[player_id] = keep
        duplicates.append((keep, drop, same_salary))

    log(f"  {CONTRACTS_FILE}: {len(duplicates)} duplicate player row(s) removed:")
    for keep, drop, same_salary in duplicates:
        note = "" if same_salary else "  <-- 2026-27 salary DIFFERS, check this one"
        log(f"    {keep['player']}: kept {keep['contract_team']} "
            f"(guaranteed {keep['guaranteed']}), dropped {drop['contract_team']} "
            f"(guaranteed {drop['guaranteed']}){note}")
    log(f"  {CONTRACTS_FILE}: {len(by_player)} players")
    return list(by_player.values())


# ---------------------------------------------------------------------------
# Writing output and checking the results
# ---------------------------------------------------------------------------

def write_csv(filename, rows):
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    path = CLEAN_DIR / filename
    # utf-8-sig so accented names also display correctly if opened in Excel
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    log(f"  wrote data/clean/{filename} ({len(rows)} rows)")


def run_checks(advanced, per_game, contracts):
    """Simple data quality checks. Any FAIL means something needs a look."""
    results = []

    keys = [(r["season"], r["player_id"]) for r in advanced]
    results.append(("Advanced: one row per player per season",
                    len(keys) == len(set(keys))))

    ids = [r["player_id"] for r in per_game]
    results.append(("Per game: one row per player", len(ids) == len(set(ids))))

    ids = [r["player_id"] for r in contracts]
    results.append(("Contracts: one row per player", len(ids) == len(set(ids))))

    missing_salary = [r["player"] for r in contracts if not r["salary_2026_27"]]
    results.append(("Contracts: every player has a 2026-27 salary above $0",
                    not missing_salary))

    seasons = {r["season"] for r in advanced}
    results.append(("Advanced: all 3 seasons present",
                    seasons == set(ADVANCED_FILES)))

    for name, passed in results:
        log(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if missing_salary:
        log(f"        missing salary: {', '.join(missing_salary)}")

    # Preview of the join we'll do next: how many contracts have 2025-26 stats?
    played_2026 = {r["player_id"] for r in advanced if r["season"] == "2025-26"}
    no_stats = [r["player"] for r in contracts if r["player_id"] not in played_2026]
    matched = len(contracts) - len(no_stats)
    log(f"\n  Contracts matched to 2025-26 stats by player ID: "
        f"{matched} of {len(contracts)}")
    log(f"  No 2025-26 stats (mostly 2026 rookies or players who sat out): "
        f"{len(no_stats)}")
    for name in no_stats:
        log(f"    - {name}")

    return all(passed for _, passed in results)


def main():
    log("NBA Contract Value Finder - cleaning raw data")
    log("=" * 50)

    log("\nAdvanced stats (3 seasons)")
    advanced = clean_advanced()

    log("\nPer game stats (2025-26)")
    per_game = clean_per_game()

    log("\nContracts")
    contracts = clean_contracts()

    log("\nWriting clean files")
    write_csv("advanced_3yr.csv", advanced)
    write_csv("per_game_2026.csv", per_game)
    write_csv("contracts.csv", contracts)

    log("\nData checks")
    all_passed = run_checks(advanced, per_game, contracts)

    log("\n" + ("All checks passed." if all_passed else "Some checks FAILED - see above."))

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    (CLEAN_DIR / "cleaning_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    print("Log saved to data/clean/cleaning_log.txt")


if __name__ == "__main__":
    main()
