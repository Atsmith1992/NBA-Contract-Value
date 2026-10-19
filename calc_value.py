"""
calc_value.py
-------------
Step 2 of the NBA Contract Value Finder pipeline.
Run clean_data.py first; this script reads its output in data/clean/.

Question: which 2026-27 contracts are the best value?

Method (the same idea as a pay equity / market pricing analysis in HR):
  1. PRODUCTION: Win Shares per season, averaged over the seasons a player
     actually played (2023-24 to 2025-26). Win Shares = the number of team
     wins a player is credited with.
  2. ELIGIBLE: at least 1,500 total minutes over the 3 seasons (so tiny
     samples can't look like steals), a 2026-27 salary, and NOT a first-round
     pick from 2023-2025 (rookie salaries are capped by league rules, so they
     aren't market prices).
  3. MARKET PAY LINE: fit a straight line through eligible players:
         salary = intercept + slope x (win shares per season)
     This is "what the league typically pays for this much production."
  4. SURPLUS VALUE = market pay for his production - his actual salary.
     Positive = underpaid (a steal). Negative = overpaid.
  5. FLAGS, not deletions: players who missed all of 2025-26 stay in but
     are flagged, so injury risk is visible instead of hidden.
  6. CONTEXT COLUMNS: contract type (veteran minimum, max-level, standard,
     rookie scale) and age band. They don't change any score. They explain
     the outliers: old players on minimum deals look like steals because the
     model looks backward, and max players hit the league's salary ceiling.

Output:
  data/clean/player_value.csv   one row per contracted player with stats
  data/clean/value_log.txt      the pay line, its fit, and the top/bottom lists

Uses only the Python standard library.
Run it from the repo folder:   python calc_value.py
"""

import csv
import statistics
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings (the rules of the analysis, all in one place)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CLEAN_DIR = BASE_DIR / "data" / "clean"

SEASONS = ["2023-24", "2024-25", "2025-26"]
LATEST_SEASON = "2025-26"
MIN_TOTAL_MINUTES = 1500

# The lowest 2026-27 salary on a standard deal in our contracts data (the
# rookie minimum). The pay line never predicts less than this, because no
# one can be paid less than the minimum.
LEAGUE_MINIMUM = 1_357_763

# Value tiers by surplus value, in dollars
TIERS = [
    (10_000_000, "Steal"),         # $10M+ underpaid
    (2_000_000, "Good value"),     # $2M to $10M underpaid
    (-2_000_000, "Fair"),          # within $2M of the line
    (-10_000_000, "Overpaid"),     # $2M to $10M overpaid
]
LOWEST_TIER = "Big overpay"        # more than $10M overpaid

# Contract type rules (used for context, not for scoring)
# Max-level: around the smallest max contract (25% of the salary cap).
# This is an approximation; adjust it if you use a different cap figure.
MAX_LEVEL_FLOOR = 40_000_000
# Veteran minimum: the largest minimum salary in our contracts data is
# $3,876,529 (the minimum for players with 10+ years of service), so
# anything at or below ~$4M for a player aged 30+ is a minimum-type deal.
VET_MIN_CEILING = 4_000_000
VET_MIN_AGE = 30

# Age bands (age during 2025-26)
AGE_BANDS = [(24, "Under 25"), (29, "25-29"), (33, "30-33")]
OLDEST_BAND = "34+"

log_lines = []


def log(message=""):
    print(message)
    log_lines.append(message)


# ---------------------------------------------------------------------------
# Reading the clean files
# ---------------------------------------------------------------------------

def read_csv(filename):
    with open(CLEAN_DIR / filename, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(text):
    """'2.5' -> 2.5, '' -> None."""
    return float(text) if text not in ("", None) else None


# ---------------------------------------------------------------------------
# Step 1: production per player
# ---------------------------------------------------------------------------

def summarize_production(advanced_rows):
    """Turn 1-3 season rows per player into one summary row per player."""
    by_player = {}
    for row in advanced_rows:
        by_player.setdefault(row["player_id"], []).append(row)

    summary = {}
    for pid, rows in by_player.items():
        ws_by_season = {r["season"]: num(r["ws"]) for r in rows}
        ws_values = list(ws_by_season.values())
        latest = max(rows, key=lambda r: r["season"])  # most recent season

        summary[pid] = {
            "player_id": pid,
            "player": latest["player"],
            "pos": latest["pos"],
            # If he missed 2025-26, add the years since his last season
            "age_2025_26": int(latest["age"])
                           + int(LATEST_SEASON[:4]) - int(latest["season"][:4]),
            "latest_team": latest["team"],
            "seasons_played": len(rows),
            "total_minutes": sum(int(r["minutes"]) for r in rows),
            "ws_2023_24": ws_by_season.get("2023-24"),
            "ws_2024_25": ws_by_season.get("2024-25"),
            "ws_2025_26": ws_by_season.get("2025-26"),
            "ws_per_season": round(sum(ws_values) / len(ws_values), 2),
            "vorp_per_season": round(sum(num(r["vorp"]) for r in rows) / len(rows), 2),
            # Consistency: how much WS bounced around year to year (lower = steadier)
            "ws_spread": round(statistics.pstdev(ws_values), 2) if len(ws_values) > 1 else None,
            "missed_2025_26": "Yes" if LATEST_SEASON not in ws_by_season else "No",
        }
    return summary


# ---------------------------------------------------------------------------
# Step 3: the market pay line (simple linear regression, by hand)
# ---------------------------------------------------------------------------

def fit_line(xs, ys):
    """
    Least-squares straight line through the points (x, y).
    slope = how much y rises for each 1 unit of x
    r_squared = share of the variation in y the line explains (0 to 1)
    """
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    ss_total = sum((y - mean_y) ** 2 for y in ys)
    ss_resid = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r_squared = 1 - ss_resid / ss_total
    return intercept, slope, r_squared


def tier_for(surplus):
    for floor, label in TIERS:
        if surplus >= floor:
            return label
    return LOWEST_TIER


def age_band_for(age):
    for top_age, label in AGE_BANDS:
        if age <= top_age:
            return label
    return OLDEST_BAND


def contract_type_for(row):
    """Label the kind of deal, so outliers can be explained or filtered."""
    if row["rookie_scale"] != "No":
        return "Rookie scale"
    if row["salary_2026_27"] >= MAX_LEVEL_FLOOR:
        return "Max-level"
    if row["salary_2026_27"] <= VET_MIN_CEILING and row["age_2025_26"] >= VET_MIN_AGE:
        return "Veteran minimum"
    return "Standard"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log("NBA Contract Value Finder - value calculation")
    log("=" * 50)

    production = summarize_production(read_csv("advanced_3yr.csv"))
    contracts = read_csv("contracts.csv")
    per_game = {r["player_id"]: r for r in read_csv("per_game_2026.csv")}
    rookie_ids = {r["player_id"]: r for r in read_csv("draft_first_round.csv")
                  if r["player_id"]}

    # Join contracts to production on player ID
    players = []
    no_stats = 0
    for c in contracts:
        p = production.get(c["player_id"])
        if p is None:
            no_stats += 1  # mostly 2026 rookies: no production to measure
            continue
        row = dict(p)
        row["salary_2026_27"] = int(c["salary_2026_27"])
        row["contract_team"] = c["contract_team"]
        row["seasons_listed"] = int(c["seasons_listed"])

        pg = per_game.get(c["player_id"], {})
        row["ppg_2025_26"] = num(pg.get("ppg"))
        row["rpg_2025_26"] = num(pg.get("rpg"))
        row["apg_2025_26"] = num(pg.get("apg"))

        draft = rookie_ids.get(c["player_id"])
        row["rookie_scale"] = (f"Yes ({draft['draft_year']} #{draft['pick']})"
                               if draft else "No")
        row["contract_type"] = contract_type_for(row)
        row["age_band"] = age_band_for(row["age_2025_26"])

        # Step 2: eligibility, with the reason recorded when excluded
        if draft:
            row["exclusion_reason"] = "Rookie-scale contract"
        elif row["total_minutes"] < MIN_TOTAL_MINUTES:
            row["exclusion_reason"] = f"Under {MIN_TOTAL_MINUTES:,} minutes"
        else:
            row["exclusion_reason"] = ""
        row["eligible"] = "Yes" if not row["exclusion_reason"] else "No"
        players.append(row)

    eligible = [p for p in players if p["eligible"] == "Yes"]
    log(f"\nContracted players with stats: {len(players)} "
        f"({no_stats} contracts had no stats and were skipped)")
    for reason in ("Rookie-scale contract", f"Under {MIN_TOTAL_MINUTES:,} minutes"):
        count = sum(1 for p in players if p["exclusion_reason"] == reason)
        log(f"  excluded - {reason}: {count}")
    log(f"  eligible for the pay line: {len(eligible)}")

    # Step 3: fit the market pay line on eligible players only
    xs = [p["ws_per_season"] for p in eligible]
    ys = [p["salary_2026_27"] for p in eligible]
    intercept, slope, r_squared = fit_line(xs, ys)
    log("\nMarket pay line")
    log(f"  salary = ${intercept / 1e6:.1f}M + ${slope / 1e6:.2f}M x (win shares per season)")
    log(f"  In words: each extra win share per season is worth about ${slope / 1e6:.1f}M in salary.")
    log(f"  R-squared = {r_squared:.2f} -> production explains about "
        f"{r_squared:.0%} of the differences in pay.")

    # Step 4: expected salary, surplus value, tier (eligible players only)
    for p in players:
        if p["eligible"] == "Yes":
            expected = max(intercept + slope * p["ws_per_season"], LEAGUE_MINIMUM)
            p["market_salary"] = round(expected)
            p["surplus_value"] = round(expected - p["salary_2026_27"])
            p["value_tier"] = tier_for(p["surplus_value"])
        else:
            p["market_salary"] = p["surplus_value"] = None
            p["value_tier"] = "Not rated"

    # Column order for the output file (and Power BI)
    columns = [
        "player_id", "player", "pos", "age_2025_26", "age_band",
        "latest_team", "contract_team", "contract_type",
        "salary_2026_27", "market_salary", "surplus_value", "value_tier",
        "ws_per_season", "vorp_per_season", "ws_2023_24", "ws_2024_25", "ws_2025_26",
        "ws_spread", "seasons_played", "total_minutes", "missed_2025_26",
        "ppg_2025_26", "rpg_2025_26", "apg_2025_26",
        "seasons_listed", "rookie_scale", "eligible", "exclusion_reason",
    ]
    players.sort(key=lambda p: (p["surplus_value"] is None, -(p["surplus_value"] or 0)))
    with open(CLEAN_DIR / "player_value.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(players)
    log(f"\nWrote data/clean/player_value.csv ({len(players)} rows)")

    # Tier counts and the top / bottom 10
    log("\nValue tiers (eligible players)")
    for label in [t[1] for t in TIERS] + [LOWEST_TIER]:
        log(f"  {label}: {sum(1 for p in eligible if p['value_tier'] == label)}")

    def show(title, rows):
        log(f"\n{title}")
        for p in rows:
            flag = "  [missed 2025-26]" if p["missed_2025_26"] == "Yes" else ""
            log(f"  {p['player']:<26} salary ${p['salary_2026_27'] / 1e6:5.1f}M  "
                f"market ${p['market_salary'] / 1e6:5.1f}M  "
                f"surplus {p['surplus_value'] / 1e6:+6.1f}M{flag}")

    log("\nContract types (eligible players): average surplus value")
    for ctype in ("Standard", "Veteran minimum", "Max-level"):
        group = [p["surplus_value"] for p in eligible if p["contract_type"] == ctype]
        if group:
            log(f"  {ctype:<16} {len(group):>3} players, "
                f"average surplus {statistics.fmean(group) / 1e6:+.1f}M")

    ranked = sorted(eligible, key=lambda p: p["surplus_value"], reverse=True)
    show("Top 10 best-value contracts (all)", ranked[:10])
    show("Top 10 most overpaid contracts (all)", ranked[-10:][::-1])

    # The "real" steals: normal market contracts for players still in their prime
    standard_prime = [p for p in ranked
                      if p["contract_type"] == "Standard" and p["age_band"] != OLDEST_BAND]
    show("Top 10 best-value STANDARD contracts, age under 34", standard_prime[:10])

    (CLEAN_DIR / "value_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    print("\nLog saved to data/clean/value_log.txt")


if __name__ == "__main__":
    main()
