"""
fred_extract.py
================
Pull every FRED series for the U.S. Macro Health Dashboard and shape the data
into a clean star schema ready for Power BI:

    fact_observations.csv  -> long/tidy fact table (date, series_id, value)
    dim_series.csv         -> one row per series (id, name, theme, units, freq)
    dim_calendar.csv       -> daily date dimension covering the full range

Usage
-----
    export FRED_API_KEY="your_key_here"      # get a free key at
                                             # https://fredaccount.stlouisfed.org/apikeys
    python fred_extract.py                    # writes CSVs to ./output
    python fred_extract.py --start 2000-01-01 --outdir data

Requirements
------------
    pip install fredapi pandas
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fred_extract")


# --------------------------------------------------------------------------- #
# Series catalogue — grouped by dashboard page/theme.
# Edit this block to add or remove series; everything downstream adapts.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Series:
    fred_id: str
    name: str          # friendly name shown in the dashboard
    theme: str         # dashboard page the series belongs to


SERIES: list[Series] = [
    # --- Inflation ---------------------------------------------------------
    Series("CPIAUCSL",             "Headline CPI",                 "Inflation"),
    Series("CPILFESL",             "Core CPI",                     "Inflation"),
    Series("PCEPI",                "Headline PCE",                 "Inflation"),
    Series("PCEPILFE",             "Core PCE (Fed target)",        "Inflation"),
    Series("CORESTICKM159SFRBATL", "Sticky CPI",                   "Inflation"),
    Series("COREFLEXCPIM159SFRBATL", "Flexible CPI",               "Inflation"),
    Series("CUSR0000SAH1",         "Shelter CPI",                  "Inflation"),
    Series("CUSR0000SACL1E",       "Core Goods CPI",               "Inflation"),
    Series("CUSR0000SASLE",        "Core Services CPI",            "Inflation"),
    Series("CPIUFDSL",             "Food CPI",                     "Inflation"),
    Series("CPIENGSL",             "Energy CPI",                   "Inflation"),
    Series("PPIFIS",               "PPI Final Demand",             "Inflation"),
    Series("T5YIFR",               "5Y5Y Forward Inflation",       "Inflation"),
    Series("ECIALLCIV",            "Employment Cost Index",        "Inflation"),
    # --- Labor market ------------------------------------------------------
    Series("UNRATE",               "Unemployment Rate",            "Labor"),
    Series("PAYEMS",               "Nonfarm Payrolls",             "Labor"),
    Series("CIVPART",              "Labor Force Participation",    "Labor"),
    Series("JTSJOL",               "Job Openings (JOLTS)",         "Labor"),
    Series("UNEMPLOY",             "Unemployed (level)",           "Labor"),
    Series("ICSA",                 "Initial Jobless Claims",       "Labor"),
    Series("CCSA",                 "Continuing Jobless Claims",    "Labor"),
    # --- Rates & recession -------------------------------------------------
    Series("FEDFUNDS",             "Fed Funds Rate",               "Rates"),
    Series("DFEDTARU",             "Fed Funds Target (upper)",     "Rates"),
    Series("T10Y2Y",               "10Y-2Y Spread",                "Rates"),
    Series("T10Y3M",               "10Y-3M Spread",                "Rates"),
    Series("DGS2",                 "2-Year Treasury",              "Rates"),
    Series("DGS10",                "10-Year Treasury",             "Rates"),
    Series("USREC",                "NBER Recession Indicator",     "Rates"),
    Series("SAHMREALTIME",         "Sahm Rule (real-time)",        "Rates"),
    # --- Growth ------------------------------------------------------------
    Series("GDPC1",                "Real GDP",                     "Growth"),
]


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_one(fred, spec: Series, start: str) -> pd.DataFrame:
    """Fetch a single series and return it in long format.

    Returns an empty DataFrame (not an exception) on failure so that one bad
    series never aborts the whole run.
    """
    try:
        raw = fred.get_series(spec.fred_id, observation_start=start)
    except Exception as exc:  # noqa: BLE001 - we want to keep going
        log.warning("  ! %-22s failed: %s", spec.fred_id, exc)
        return pd.DataFrame(columns=["date", "series_id", "value"])

    df = (
        raw.rename("value")
        .rename_axis("date")
        .reset_index()
        .dropna(subset=["value"])
    )
    df["series_id"] = spec.fred_id
    log.info("  + %-22s %5d rows", spec.fred_id, len(df))
    return df[["date", "series_id", "value"]]


def fetch_series_info(fred, fred_id: str) -> dict:
    """Best-effort metadata pull for the series dimension."""
    try:
        info = fred.get_series_info(fred_id)
        return {
            "units": info.get("units_short", ""),
            "frequency": info.get("frequency_short", ""),
            "seasonal_adj": info.get("seasonal_adjustment_short", ""),
            "last_updated": info.get("last_updated", ""),
        }
    except Exception:  # noqa: BLE001
        return {"units": "", "frequency": "", "seasonal_adj": "", "last_updated": ""}


# --------------------------------------------------------------------------- #
# Shape into star schema
# --------------------------------------------------------------------------- #
def build_fact(frames: list[pd.DataFrame]) -> pd.DataFrame:
    fact = pd.concat(frames, ignore_index=True)
    fact["date"] = pd.to_datetime(fact["date"])
    fact["value"] = pd.to_numeric(fact["value"], errors="coerce")
    return fact.dropna(subset=["value"]).sort_values(["series_id", "date"])


def build_dim_series(meta: dict[str, dict]) -> pd.DataFrame:
    rows = [
        {
            "series_id": s.fred_id,
            "series_name": s.name,
            "theme": s.theme,
            **meta.get(s.fred_id, {}),
        }
        for s in SERIES
    ]
    return pd.DataFrame(rows)


def build_calendar(fact: pd.DataFrame) -> pd.DataFrame:
    """Daily date dimension spanning the full data range.

    A daily grain keeps the model flexible across mixed frequencies
    (daily yields, monthly CPI, quarterly GDP).
    """
    start, end = fact["date"].min(), fact["date"].max()
    cal = pd.DataFrame({"date": pd.date_range(start, end, freq="D")})
    cal["year"] = cal["date"].dt.year
    cal["quarter"] = cal["date"].dt.quarter
    cal["month"] = cal["date"].dt.month
    cal["month_name"] = cal["date"].dt.strftime("%b")
    cal["year_month"] = cal["date"].dt.strftime("%Y-%m")
    cal["month_start"] = cal["date"].dt.to_period("M").dt.start_time
    return cal


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(api_key: str, start: str, outdir: str) -> None:
    from fredapi import Fred

    fred = Fred(api_key=api_key)
    os.makedirs(outdir, exist_ok=True)

    log.info("Fetching %d series from FRED (start=%s)…", len(SERIES), start)
    frames, meta = [], {}
    for spec in SERIES:
        df = fetch_one(fred, spec, start)
        if not df.empty:
            frames.append(df)
        meta[spec.fred_id] = fetch_series_info(fred, spec.fred_id)
        time.sleep(0.3)  # stay well under FRED's 120 req/min limit

    if not frames:
        log.error("No data fetched — check your API key and network.")
        sys.exit(1)

    fact = build_fact(frames)
    dim_series = build_dim_series(meta)
    dim_calendar = build_calendar(fact)

    paths = {
        "fact_observations.csv": fact,
        "dim_series.csv": dim_series,
        "dim_calendar.csv": dim_calendar,
    }
    for fname, df in paths.items():
        path = os.path.join(outdir, fname)
        df.to_csv(path, index=False)
        log.info("Wrote %-24s %6d rows -> %s", fname, len(df), path)

    log.info(
        "Done. %d series, %d observations, %s to %s.",
        fact["series_id"].nunique(),
        len(fact),
        fact["date"].min().date(),
        fact["date"].max().date(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract FRED series for the macro dashboard.")
    parser.add_argument("--api-key", default=os.environ.get("FRED_API_KEY"),
                        help="FRED API key (or set FRED_API_KEY env var).")
    parser.add_argument("--start", default="1990-01-01",
                        help="Earliest observation date (YYYY-MM-DD).")
    parser.add_argument("--outdir", default="output",
                        help="Directory for the CSV output.")
    args = parser.parse_args()

    if not args.api_key:
        parser.error(
            "No API key. Set FRED_API_KEY or pass --api-key. "
            "Get a free key at https://fredaccount.stlouisfed.org/apikeys"
        )
    run(args.api_key, args.start, args.outdir)


if __name__ == "__main__":
    main()
