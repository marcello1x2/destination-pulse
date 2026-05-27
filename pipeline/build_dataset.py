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

TARGET_CITIES = {
    "amsterdam", "barcelona", "bordeaux", "copenhagen", "como", "dubrovnik",
    "florence", "kyoto", "lecco", "lisbon", "lucerne", "milan", "nice",
    "palma", "queenstown", "reykjavik", "salzburg", "san sebastian",
    "seville", "singapore"
}

CITY_SEGMENTS = {
    "como": "Leisure / resort", "lecco": "Leisure / resort", "florence": "Urban culture",
    "kyoto": "Urban culture", "barcelona": "Urban culture", "lisbon": "Urban culture",
    "dubrovnik": "Leisure / resort", "palma": "Leisure / resort", "nice": "Leisure / resort",
    "salzburg": "Urban culture", "lucerne": "Leisure / resort", "amsterdam": "Urban culture",
    "copenhagen": "Urban culture", "milan": "Urban culture", "seville": "Urban culture",
    "singapore": "Urban culture", "reykjavik": "Leisure / resort", "bordeaux": "Urban culture",
    "queenstown": "Leisure / resort", "san sebastian": "Leisure / resort"
}

CITY_PREMIUM = {
    "como": 94, "lecco": 86, "florence": 95, "kyoto": 96, "barcelona": 90,
    "lisbon": 86, "dubrovnik": 91, "palma": 89, "nice": 91, "salzburg": 89,
    "lucerne": 95, "amsterdam": 88, "copenhagen": 87, "milan": 84, "seville": 84,
    "singapore": 92, "reykjavik": 90, "bordeaux": 88, "queenstown": 93, "san sebastian": 92
}

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "premium-cities-dataset/1.0"
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


def income_proxy(level):
    mapping = {
        "High income": 95,
        "Upper middle income": 76,
        "Lower middle income": 58,
        "Low income": 42,
    }
    return mapping.get(level, 60)


def tourism_proxy(arrivals, population):
    if not arrivals or not population or population <= 0:
        return 55
    ratio = arrivals / population
    score = 45 + min(ratio, 5) * 11
    return round(max(40, min(95, score)))


def city_size_score(pop):
    if not pop or pop <= 0:
        return 60
    return max(55, 100 - abs(math.log10(pop) - 5.5) * 18)


def pm25_to_score(pm25, fallback=50):
    if pm25 in (None, ""):
        return fallback
    try:
        x = float(pm25)
    except Exception:
        return fallback
    x = max(0.0, min(x, 35.0))
    score = 100 - (x / 35.0) * 100
    return round(max(20, min(100, score)))


def final_score(tourism, income, premium, city_pop, air_quality_score):
    return round(
        tourism * 0.30 +
        income * 0.18 +
        premium * 0.30 +
        city_size_score(city_pop) * 0.07 +
        air_quality_score * 0.15
    )


def compute_tier(score):
    if score >= 90:
        return "Iconic"
    if score >= 84:
        return "Premium"
    return "Curated"


def geonames_coords(geonameid):
    if pd.isna(geonameid):
        return None, None

    url = GEONAMES_DETAILS_URL.format(geonameid=int(geonameid))
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        lat = data.get("lat")
        lon = data.get("lng")
        if lat is None or lon is None:
            return None, None
        return float(lat), float(lon)
    except Exception:
        return None, None


def get_nearest_openaq_location(lat, lon, iso2=None, radius_m=25000):
    if not OPENAQ_API_KEY or lat is None or lon is None:
        return None

    params = {
        "coordinates": f"{lat:.4f},{lon:.4f}",
        "radius": radius_m,
        "limit": 25,
        "order_by": "distance",
        "sort_order": "asc"
    }

    if iso2 and iso2 != "NA":
        params["iso"] = iso2

    try:
        r = session.get(f"{OPENAQ_BASE}/locations", params=params, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None

        for loc in results:
            sensors = loc.get("sensors", []) or []
            for sensor in sensors:
                parameter = (sensor.get("parameter") or {}).get("name")
                if parameter == "pm25":
                    return loc

        return results[0]
    except Exception:
        return None


def get_latest_pm25_for_location(location_id):
    if not OPENAQ_API_KEY or not location_id:
        return None, None

    try:
        r = session.get(f"{OPENAQ_BASE}/locations/{location_id}/latest", params={"limit": 100}, timeout=30)
        r.raise_for_status()
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


def main():
    cities = pd.read_csv(WORLD_CITIES_URL)
    cols = {c.lower(): c for c in cities.columns}
    city_col = cols.get("name") or cols.get("city")
    country_col = cols.get("country")
    subcountry_col = cols.get("subcountry")
    geonameid_col = cols.get("geonameid")

    if not city_col or not country_col or not geonameid_col:
        raise ValueError(f"Unexpected city columns: {cities.columns.tolist()}")

    cities["city_lc"] = cities[city_col].astype(str).str.strip().str.lower()
    cities = cities[cities["city_lc"].isin(TARGET_CITIES)].copy()

    countries = country_lookup()
    arrivals_map = latest_values(WB_TOURISM_URL)
    wb_pop_map = latest_values(WB_POP_URL)

    out_rows = []
    for _, row in cities.iterrows():
        city_name = str(row[city_col]).strip()
        country_name = str(row[country_col]).strip()
        city_lc = city_name.lower()

        wb = countries.get(country_name, {})
        iso2 = wb.get("iso2")
        iso3 = wb.get("iso3")

        country_pop = wb_pop_map.get(iso3)
        arrivals = arrivals_map.get(iso3)

        tourism = tourism_proxy(arrivals, country_pop)
        income = income_proxy(wb.get("income_level"))
        premium = CITY_PREMIUM.get(city_lc, 75)
        city_population = None

        lat, lon = geonames_coords(row[geonameid_col])
        time.sleep(0.15)

        pm25 = None
        pm25_units = None
        pm25_source = None
        aq_score = 50

        if OPENAQ_API_KEY and lat is not None and lon is not None:
            loc = get_nearest_openaq_location(lat, lon, iso2=iso2, radius_m=25000)
            time.sleep(0.2)

            if loc:
                pm25, pm25_units = get_latest_pm25_for_location(loc.get("id"))
                time.sleep(0.2)
                if pm25 is not None:
                    pm25_source = "OpenAQ"
                    aq_score = pm25_to_score(pm25, fallback=50)
                else:
                    pm25_source = "OpenAQ-no-pm25"
            else:
                pm25_source = "OpenAQ-no-location"
        else:
            pm25_source = "OpenAQ-disabled"

        item = {
            "city": city_name,
            "country": country_name,
            "region": str(row[subcountry_col]).strip() if subcountry_col else None,
            "segment": CITY_SEGMENTS.get(city_lc, "Urban culture"),
            "premium": premium,
            "tourism": tourism,
            "income": income,
            "country_iso3": iso3,
            "country_iso2": iso2,
            "country_income_level": wb.get("income_level"),
            "country_region": wb.get("region"),
            "country_arrivals_latest": arrivals,
            "country_population_latest": country_pop,
            "population": city_population,
            "lat": lat,
            "lon": lon,
            "air_quality_pm25": pm25,
            "air_quality_units": pm25_units,
            "air_quality_score": aq_score,
            "air_quality_source": pm25_source,
            "source_city_registry": WORLD_CITIES_URL,
            "source_worldbank_tourism": WB_TOURISM_URL,
            "source_worldbank_population": WB_POP_URL,
            "source_geonames_lookup": "https://www.geonames.org/",
            "source_openaq": "https://api.openaq.org/v3"
        }

        item["score"] = final_score(
            tourism=tourism,
            income=income,
            premium=premium,
            city_pop=city_population,
            air_quality_score=aq_score
        )
        item["tier"] = compute_tier(item["score"])
        out_rows.append(item)

    out_rows.sort(key=lambda x: x["score"], reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v4-live-openaq-enriched",
        "sources": {
            "city_registry": WORLD_CITIES_URL,
            "worldbank_country": WB_COUNTRY_URL,
            "worldbank_tourism": WB_TOURISM_URL,
            "worldbank_population": WB_POP_URL,
            "geonames_lookup": "https://www.geonames.org/",
            "openaq": "https://api.openaq.org/v3"
        },
        "notes": [
            "City registry is loaded from a public world cities CSV sourced from GeoNames.",
            "Coordinates are resolved from GeoNames using geonameid.",
            "Tourism score is derived from latest available World Bank arrivals divided by country population.",
            "Income score is derived from World Bank income level.",
            "Air quality uses nearest available OpenAQ PM2.5 observation within 25km when available."
        ],
        "cities": out_rows
    }

    root = Path(__file__).resolve().parents[1]
    out = root / "data" / "cities-live.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Cities: {len(out_rows)}")
    print(f"OpenAQ enabled: {'yes' if OPENAQ_API_KEY else 'no'}")


if __name__ == "__main__":
    main()
