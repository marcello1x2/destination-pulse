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

OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY")
OPENAQ_BASE = "https://api.openaq.org/v3"
GEONAMES_DETAILS_URL = "https://www.geonames.org/getJSON?geonameId={geonameid}"

TOP_N = 20
MIN_COUNTRY_INCOME = {"High income", "Upper middle income"}

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "destination-pulse/2.0"
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
        income = row.get("incomeLevel", {}).get("value")
        region = row.get("region", {}).get("value")
        if name and iso3 and iso2:
            by_name[name] = {
                "iso2": iso2,
                "iso3": iso3,
                "income_level": income,
                "region": region,
            }
    return by_name


def income_score(level):
    mapping = {
        "High income": 100,
        "Upper middle income": 75,
        "Lower middle income": 50,
        "Low income": 25,
    }
    return mapping.get(level, 40)


def tourism_score(arrivals, population):
    if not arrivals or not population or population <= 0:
        return 45
    ratio = arrivals / population
    score = 40 + min(ratio, 5) * 12
    return round(max(30, min(100, score)), 2)


def city_size_score(pop):
    if not pop or pop <= 0:
        return 55
    return round(max(40, min(100, 100 - abs(math.log10(pop) - 5.7) * 20)), 2)


def geonames_details(geonameid):
    if pd.isna(geonameid):
        return {"lat": None, "lon": None, "population": None}

    try:
        url = GEONAMES_DETAILS_URL.format(geonameid=int(geonameid))
        r = session.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        lat = data.get("lat")
        lon = data.get("lng")
        pop = data.get("population")
        return {
            "lat": float(lat) if lat not in (None, "") else None,
            "lon": float(lon) if lon not in (None, "") else None,
            "population": int(pop) if pop not in (None, "", "0") else None
        }
    except Exception:
        return {"lat": None, "lon": None, "population": None}


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


def get_candidate_location(city_name, iso2, lat, lon):
    if not OPENAQ_API_KEY:
        return None

    city_queries = [{"city": city_name, "limit": 25}]
    if iso2 and iso2 != "NA":
        city_queries.insert(0, {"city": city_name, "iso": iso2, "limit": 25})

    for params in city_queries:
        results = get_openaq_locations(params)
        for loc in results:
            sensors = loc.get("sensors", []) or []
            for sensor in sensors:
                parameter = (sensor.get("parameter") or {}).get("name")
                if parameter == "pm25":
                    return loc

    if lat is not None and lon is not None:
        geo_queries = [{
            "coordinates": f"{lat:.4f},{lon:.4f}",
            "radius": 25000,
            "limit": 50,
            "order_by": "distance",
            "sort_order": "asc"
        }]
        if iso2 and iso2 != "NA":
            geo_queries.insert(0, {
                "coordinates": f"{lat:.4f},{lon:.4f}",
                "radius": 25000,
                "limit": 50,
                "order_by": "distance",
                "sort_order": "asc",
                "iso": iso2
            })

        for params in geo_queries:
            results = get_openaq_locations(params)
            for loc in results:
                sensors = loc.get("sensors", []) or []
                for sensor in sensors:
                    parameter = (sensor.get("parameter") or {}).get("name")
                    if parameter == "pm25":
                        return loc

    return None


def get_latest_pm25_for_location(location_id):
    if not OPENAQ_API_KEY or not location_id:
        return None, None

    try:
        r = session.get(f"{OPENAQ_BASE}/locations/{location_id}/latest", params={"limit": 100}, timeout=30)
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


def pm25_to_score(pm25, fallback=50):
    if pm25 in (None, ""):
        return fallback
    try:
        x = float(pm25)
    except Exception:
        return fallback

    x = max(0.0, min(x, 35.0))
    score = 100 - (x / 35.0) * 100
    return round(max(20, min(100, score)), 2)


def clean_region(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return None
    return text


def final_score(tourism, income, city_pop, aq_score, has_real_aq):
    aq_bonus = 5 if has_real_aq else 0
    return round(
        tourism * 0.42 +
        income * 0.28 +
        city_size_score(city_pop) * 0.20 +
        aq_score * 0.10 +
        aq_bonus,
        2
    )


def compute_tier(score):
    if score >= 85:
        return "Top"
    if score >= 75:
        return "Strong"
    return "Watch"


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

    countries = country_lookup()
    arrivals_map = latest_values(WB_TOURISM_URL)
    wb_pop_map = latest_values(WB_POP_URL)

    out_rows = []
    for _, row in cities.iterrows():
        city_name = row["city"]
        country_name = row["country"]
        region_name = row["region"] if subcountry_col else None
        wb = countries.get(country_name, {})

        income_level = wb.get("income_level")
        if income_level not in MIN_COUNTRY_INCOME:
            continue

        iso2 = wb.get("iso2")
        iso3 = wb.get("iso3")
        arrivals = arrivals_map.get(iso3)
        country_pop = wb_pop_map.get(iso3)

        geo = geonames_details(row[geonameid_col])
        lat = geo["lat"]
        lon = geo["lon"]
        city_pop = geo["population"]

        tourism = tourism_score(arrivals, country_pop)
        income = income_score(income_level)

        pm25 = None
        pm25_units = None
        pm25_source = "OpenAQ-disabled"
        aq_score = 50
        has_real_aq = False

        if OPENAQ_API_KEY:
            loc = get_candidate_location(city_name, iso2, lat, lon)
            time.sleep(0.05)

            if loc:
                pm25, pm25_units = get_latest_pm25_for_location(loc.get("id"))
                time.sleep(0.05)
                if pm25 is not None:
                    pm25_source = "OpenAQ"
                    aq_score = pm25_to_score(pm25, fallback=50)
                    has_real_aq = True
                else:
                    pm25_source = "OpenAQ-no-pm25"
            else:
                pm25_source = "OpenAQ-no-location"

        score = final_score(
            tourism=tourism,
            income=income,
            city_pop=city_pop,
            aq_score=aq_score,
            has_real_aq=has_real_aq
        )

        item = {
            "city": city_name,
            "country": country_name,
            "region": region_name,
            "country_iso3": iso3,
            "country_iso2": iso2,
            "country_income_level": income_level,
            "country_region": wb.get("region"),
            "country_arrivals_latest": arrivals,
            "country_population_latest": country_pop,
            "population": city_pop,
            "lat": lat,
            "lon": lon,
            "tourism_score": tourism,
            "income_score": income,
            "air_quality_pm25": pm25,
            "air_quality_units": pm25_units,
            "air_quality_score": aq_score,
            "air_quality_source": pm25_source,
            "has_real_air_quality": has_real_aq,
            "score": score,
            "tier": compute_tier(score),
            "source_city_registry": WORLD_CITIES_URL,
            "source_worldbank_tourism": WB_TOURISM_URL,
            "source_worldbank_population": WB_POP_URL,
            "source_geonames_lookup": "https://www.geonames.org/",
            "source_openaq": "https://api.openaq.org/v3"
        }
        out_rows.append(item)

    out_rows.sort(
        key=lambda x: (
            x["score"],
            x["has_real_air_quality"],
            x["tourism_score"],
            x["income_score"],
            x["population"] or 0
        ),
        reverse=True
    )

    top_rows = out_rows[:TOP_N]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v6-real-top20",
        "sources": {
            "city_registry": WORLD_CITIES_URL,
            "worldbank_country": WB_COUNTRY_URL,
            "worldbank_tourism": WB_TOURISM_URL,
            "worldbank_population": WB_POP_URL,
            "geonames_lookup": "https://www.geonames.org/",
            "openaq": "https://api.openaq.org/v3"
        },
        "notes": [
            "This output keeps only the top 20 cities by a real-first score.",
            "Manual premium and manual segment fields were removed.",
            "Cities come from the DataHub world-cities registry sourced from GeoNames.",
            "Coordinates and city population are resolved from GeoNames using geonameid.",
            "Tourism and income remain country-level proxies from World Bank.",
            "Air quality is real only when OpenAQ returns PM2.5 for a city/location query or 25km geospatial fallback."
        ],
        "cities": top_rows
    }

    root = Path(__file__).resolve().parents[1]
    out = root / "data" / "cities-live.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    real_aq_hits = sum(1 for x in top_rows if x["has_real_air_quality"])
    print(f"Wrote {out}")
    print(f"Candidate cities scored: {len(out_rows)}")
    print(f"Top cities kept: {len(top_rows)}")
    print(f"OpenAQ enabled: {'yes' if OPENAQ_API_KEY else 'no'}")
    print(f"Top-20 with real AQ: {real_aq_hits}")


if __name__ == "__main__":
    main()
