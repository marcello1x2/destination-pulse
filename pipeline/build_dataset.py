from pathlib import Path
from datetime import datetime, timezone
import json
import math
import requests
import pandas as pd

WORLD_CITIES_URL = "https://datahub.io/core/world-cities/r/world-cities.csv"
WB_COUNTRY_URL = "https://api.worldbank.org/v2/country/all?format=json&per_page=400"
WB_TOURISM_URL = "https://api.worldbank.org/v2/country/all/indicator/ST.INT.ARVL?format=json&per_page=20000"
WB_POP_URL = "https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?format=json&per_page=20000"

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

def get_json(url):
    r = requests.get(url, timeout=60)
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

def final_score(tourism, income, premium, city_pop):
    return round(
        tourism * 0.38 +
        income * 0.24 +
        premium * 0.28 +
        city_size_score(city_pop) * 0.10
    )

def compute_tier(score):
    if score >= 90:
        return "Iconic"
    if score >= 84:
        return "Premium"
    return "Curated"

def main():
    cities = pd.read_csv(WORLD_CITIES_URL)
    cols = {c.lower(): c for c in cities.columns}
    city_col = cols.get("name") or cols.get("city")
    country_col = cols.get("country")
    subcountry_col = cols.get("subcountry")
    if not city_col or not country_col:
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
        iso3 = wb.get("iso3")
        country_pop = wb_pop_map.get(iso3)
        arrivals = arrivals_map.get(iso3)
        tourism = tourism_proxy(arrivals, country_pop)
        income = income_proxy(wb.get("income_level"))
        premium = CITY_PREMIUM.get(city_lc, 75)
        city_population = None

        item = {
            "city": city_name,
            "country": country_name,
            "region": str(row[subcountry_col]).strip() if subcountry_col else None,
            "segment": CITY_SEGMENTS.get(city_lc, "Urban culture"),
            "premium": premium,
            "tourism": tourism,
            "income": income,
            "country_iso3": iso3,
            "country_income_level": wb.get("income_level"),
            "country_region": wb.get("region"),
            "country_arrivals_latest": arrivals,
            "country_population_latest": country_pop,
            "population": city_population,
            "source_city_registry": WORLD_CITIES_URL,
            "source_worldbank_tourism": WB_TOURISM_URL,
            "source_worldbank_population": WB_POP_URL
        }
        item["score"] = final_score(tourism, income, premium, city_population)
        item["tier"] = compute_tier(item["score"])
        out_rows.append(item)

    out_rows.sort(key=lambda x: x["score"], reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v3-live-country-enriched",
        "sources": {
            "city_registry": WORLD_CITIES_URL,
            "worldbank_country": WB_COUNTRY_URL,
            "worldbank_tourism": WB_TOURISM_URL,
            "worldbank_population": WB_POP_URL
        },
        "notes": [
            "City registry is loaded from a public world cities CSV.",
            "Tourism score is derived from latest available World Bank arrivals divided by country population.",
            "Income score is derived from World Bank income level.",
            "Next step: add city-level air quality and POI density."
        ],
        "cities": out_rows
    }

    root = Path(__file__).resolve().parents[1]
    out = root / "data" / "cities-live.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Cities: {len(out_rows)}")

if __name__ == "__main__":
    main()