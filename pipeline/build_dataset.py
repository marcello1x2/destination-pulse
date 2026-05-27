from pathlib import Path
import json
from datetime import datetime, timezone

cities = [
    {"city": "Kyoto", "country": "Japan", "segment": "Urban culture", "tier": "Iconic", "tourism": 93, "income": 87, "premium": 96, "population": 1460000},
    {"city": "Florence", "country": "Italy", "segment": "Urban culture", "tier": "Iconic", "tourism": 91, "income": 84, "premium": 95, "population": 382000},
    {"city": "Barcelona", "country": "Spain", "segment": "Urban culture", "tier": "Iconic", "tourism": 95, "income": 81, "premium": 90, "population": 1620000},
    {"city": "Lisbon", "country": "Portugal", "segment": "Urban culture", "tier": "Rising", "tourism": 88, "income": 75, "premium": 86, "population": 545000},
    {"city": "Dubrovnik", "country": "Croatia", "segment": "Leisure / resort", "tier": "Premium", "tourism": 84, "income": 68, "premium": 91, "population": 41000},
    {"city": "Palma", "country": "Spain", "segment": "Leisure / resort", "tier": "Premium", "tourism": 86, "income": 81, "premium": 89, "population": 423000},
    {"city": "Como", "country": "Italy", "segment": "Leisure / resort", "tier": "Premium", "tourism": 82, "income": 84, "premium": 94, "population": 84000},
    {"city": "Lecco", "country": "Italy", "segment": "Leisure / resort", "tier": "Curated", "tourism": 76, "income": 84, "premium": 86, "population": 48000}
]

def compute_score(row):
    import math
    pop_score = max(55, 100 - abs(math.log10(row["population"]) - 5.5) * 18)
    return round(
        row["tourism"] * 0.38 +
        row["income"] * 0.24 +
        row["premium"] * 0.28 +
        pop_score * 0.10
    )

for row in cities:
    row["score"] = compute_score(row)

cities.sort(key=lambda x: x["score"], reverse=True)

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "version": "v1-city-level",
    "source_plan": [
        "world-cities-registry",
        "world-bank-indicators",
        "openaq-locations",
        "osm-overpass-poi"
    ],
    "cities": cities
}

output = Path(__file__).resolve().parents[1] / "data" / "cities-live.json"
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
print(f"Wrote {output}")