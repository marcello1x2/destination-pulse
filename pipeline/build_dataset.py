from pathlib import Path
from datetime import datetime, timezone
import json
import math
import os
import time
import requests
import pandas as pd

WORLD_CITIES_URL = "https://datahub.io/core/world-cities/r/world-cities.csv"

WB_COUNTRY_URL = "https://api.worldbank.org/v2/country/all?format=json&per_page=400"
WB_TOURISM_URL = "https://api.worldbank.org/v2/country/all/indicator/ST.INT.ARVL?format=json&per_page=20000"
WB_POP_URL = "https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?format=json&per_page=20000"

OPENAQ_BASE = "https://api.openaq.org/v3"
OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY")

TOP_N = 50
REQUEST_SLEEP = 0.03
MAX_CITIES_TO_SCAN = 1500

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "destination-pulse-real-only/4.0"
})
if OPENAQ_API_KEY:
    session.headers.update({"X-API-Key": OPENAQ_API_KEY})


def get_json(url):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def latest_values(url):
    payload = get_json(url)
    rows = payload[1]
    result = {}
    for row in rows:
        iso3 = row.get("countryiso3code")
        value = row.get("value")
        if not iso3 or value in (None, ""):
            continue
        if iso3 not in result:
            result[iso3] = value
    return result


def country_lookup():
    payload = get_json(WB_COUNTRY_URL)
    rows = payload[1]
    by_name = {}
    for row in rows:
        name = row.get("name")
        iso2 = row.get("iso2Code")
        iso3 = row.get("id")
        region = row.get("region", {}).get("value")
        income = row.get("incomeLevel", {}).get("value")
        capital_city = row.get("capitalCity")
        longitude = row.get("longitude")
        latitude = row.get("latitude")

        if name and iso2 and iso3:
            by_name[name] = {
                "iso2": iso2,
                "iso3": iso3,
                "region": region,
                "income_level": income,
                "capital_city": capital_city,
                "longitude": longitude,
                "latitude": latitude
            }
    return by_name


def clean_region(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "n/a"}:
        return None
    return text


def safe_float(value):
    try:
        if value in (None, "", "None"):
            return None
        return float(value)
    except Exception:
        return None


def get_openaq_locations(params):
    if not OPENAQ_API_KEY:
        return []
    try:
        r = session.get(f"{OPENAQ_BASE}/locations", params=params, timeout=30)
        if r.status_code >= 400:
            return []
        return r.json().get("results", [])
    except Exception:
        return []


def find_pm25_location(city_name):
    if not OPENAQ_API_KEY:
        return None

    results = get_openaq_locations({
        "city": city_name,
        "limit": 25,
        "parameter": "pm25"
    })

    for loc in results:
        sensors = loc.get("sensors", []) or []
        for sensor in sensors:
            parameter = (sensor.get("parameter") or {}).get("name")
            if parameter == "pm25":
                return loc

    return None


def get_latest_pm25(location_id):
    if not OPENAQ_API_KEY or not location_id:
        return None, None

    try:
        r = session.get(
            f"{OPENAQ_BASE}/locations/{location_id}/latest",
            params={"limit": 100},
            timeout=30
        )
        if r.status_code >= 400:
            return None, None

        results = r.json().get("results", [])
        for item in results:
            parameter = (item.get("parameter") or {}).get("name")
            value = item.get("value")
            units = item.get("units")
            if parameter == "pm25" and value is not None:
                return float(value), units

        return None, None
    except Exception:
        return None, None


def extract_location_coordinates(loc):
    coords = loc.get("coordinates") or {}
    lat = safe_float(coords.get("latitude"))
    lon = safe_float(coords.get("longitude"))
    return lat, lon


def pm25_score(pm25):
    if pm25 is None:
        return 0.0
    x = max(0.0, min(float(pm25), 35.0))
    return round(100 - (x / 35.0) * 100, 2)


def population_score_from_known_city_population(population):
    if population is None or population <= 0:
        return 50.0
    score = 100 - abs(math.log10(population) - 6.2) * 22
    return round(max(0.0, min(100.0, score)), 2)


def final_score(pm25, population_score):
    return round(
        pm25_score(pm25) * 0.70 +
        population_score * 0.30,
        2
    )


def compute_tier(score):
    if score >= 80:
        return "Top"
    if score >= 65:
        return "Strong"
    return "Listed"


def main():
    cities = pd.read_csv(WORLD_CITIES_URL)
    cols = {c.lower(): c for c in cities.columns}

    city_col = cols.get("name") or cols.get("city")
    country_col = cols.get("country")
    subcountry_col = cols.get("subcountry")
    geonameid_col = cols.get("geonameid")

    if not city_col or not country_col or not geonameid_col:
        raise ValueError(f"Unexpected city columns: {cities.columns.tolist()}")

    cities["city"] = cities[city_col].astype(str).str.strip()
    cities["country"] = cities[country_col].astype(str).str.strip()
    cities["region"] = cities[subcountry_col].apply(clean_region) if subcountry_col else None

    dedupe_cols = ["city", "country"]
    if subcountry_col:
        dedupe_cols.append("region")
    cities = cities.drop_duplicates(subset=dedupe_cols).copy()

    candidate_cities = cities.head(MAX_CITIES_TO_SCAN).copy()

    countries = country_lookup()
    wb_pop_map = latest_values(WB_POP_URL)
    wb_tourism_map = latest_values(WB_TOURISM_URL)

    out_rows = []

    for _, row in candidate_cities.iterrows():
        city_name = row["city"]
        country_name = row["country"]
        region_name = row["region"] if subcountry_col else None
        geonameid = row[geonameid_col]

        loc = find_pm25_location(city_name)
        time.sleep(REQUEST_SLEEP)

        if not loc:
            continue

        pm25, units = get_latest_pm25(loc.get("id"))
        time.sleep(REQUEST_SLEEP)

        if pm25 is None:
            continue

        lat, lon = extract_location_coordinates(loc)
        if lat is None or lon is None:
            continue

        wb = countries.get(country_name, {})
        iso3 = wb.get("iso3")
        iso2 = wb.get("iso2")
        population_total = wb_pop_map.get(iso3)

        # Neutral city-population score when no reliable city population is available in the pipeline.
        city_population = None
        pop_score = population_score_from_known_city_population(city_population)
        score = final_score(pm25, pop_score)

        country_context = {
            "country": country_name,
            "iso2": iso2,
            "iso3": iso3,
            "region": wb.get("region"),
            "income_level": wb.get("income_level"),
            "capital_city": wb.get("capital_city"),
            "population_total": population_total,
            "tourism_arrivals_latest": wb_tourism_map.get(iso3),
            "source_country_api": WB_COUNTRY_URL,
            "source_population_total": WB_POP_URL,
            "source_tourism_arrivals": WB_TOURISM_URL,
            "note": "Country context fields are national indicators and are not used in the city ranking score."
        }

        item = {
            "city": city_name,
            "country": country_name,
            "region": region_name,
            "geonameid": int(geonameid) if not pd.isna(geonameid) else None,
            "city_metrics": {
                "population": city_population,
                "lat": lat,
                "lon": lon,
                "air_quality_pm25": pm25,
                "air_quality_units": units,
                "air_quality_source": "OpenAQ",
                "openaq_location_id": loc.get("id"),
                "openaq_location_name": loc.get("name"),
                "pm25_score": pm25_score(pm25),
                "population_score": pop_score
            },
            "country_context": country_context,
            "score": score,
            "tier": compute_tier(score),
            "sources": {
                "city_registry": WORLD_CITIES_URL,
                "openaq": "https://api.openaq.org/v3",
                "worldbank_country": WB_COUNTRY_URL
            }
        }

        out_rows.append(item)

    out_rows.sort(
        key=lambda x: (
            x["score"],
            x["city_metrics"]["pm25_score"]
        ),
        reverse=True
    )

    top_rows = out_rows[:TOP_N]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v9-real-city-ranking-openaq-worldbank",
        "ranking_method": {
            "name": "real_only_city_score",
            "description": "Ranking uses observed PM2.5 from OpenAQ and a neutral population component when reliable city-level population is unavailable in the automated pipeline.",
            "formula": "score = 0.70 * pm25_score + 0.30 * population_score",
            "notes": [
                "Country context is included for dashboard storytelling only.",
                "World Bank indicators are national and are excluded from the ranking formula.",
                "Population score is neutral when city-level population is not reliably resolved."
            ]
        },
        "sources": {
            "city_registry": WORLD_CITIES_URL,
            "openaq": "https://api.openaq.org/v3",
            "worldbank_country": WB_COUNTRY_URL,
            "worldbank_population": WB_POP_URL,
            "worldbank_tourism": WB_TOURISM_URL
        },
        "notes": [
            "Cities are selected automatically from the global city registry.",
            "Only cities with real PM2.5 data from OpenAQ are included.",
            "Coordinates are taken from OpenAQ location metadata.",
            "World Bank fields are attached as country-level context for the dashboard and are not part of the ranking score."
        ],
        "cities": top_rows
    }

    root = Path(__file__).resolve().parents[1]
    out = root / "data" / "cities-live.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote {out}")
    print(f"Candidate cities scanned: {len(candidate_cities)}")
    print(f"Cities with real PM2.5 found: {len(out_rows)}")
    print(f"Top cities kept: {len(top_rows)}")
    print(f"OpenAQ enabled: {'yes' if OPENAQ_API_KEY else 'no'}")


if __name__ == "__main__":
    main()
