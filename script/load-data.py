#!/usr/bin/env python3
"""Load the travel + reservation sample data into DocumentDB.

This script upserts JSON-array fixtures from ./data into traveldb and creates
helpful indexes used by sample queries.  It also generates and loads 1000
synthetic documents per collection for bulk-testing purposes.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

try:
    from pymongo import ASCENDING, MongoClient
    from pymongo.errors import PyMongoError
    from pymongo.operations import ReplaceOne
except ImportError:
    print(
        "Missing dependency: pymongo. Install it with: pip install \"pymongo[srv]\"",
        file=sys.stderr,
    )
    sys.exit(1)


def load_dotenv_file(env_file: Path) -> None:
    """Load key-value pairs from .env without requiring python-dotenv."""
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def wait_for_connection(client: MongoClient, max_attempts: int = 30, delay_seconds: int = 2) -> None:
    print("Waiting for DocumentDB to accept connections...")
    for attempt in range(1, max_attempts + 1):
        try:
            client.admin.command("ping")
            print("  connected.")
            return
        except PyMongoError:
            if attempt == max_attempts:
                raise
            time.sleep(delay_seconds)


def load_json_array(file_path: Path) -> list[dict]:
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {file_path}")
    return payload


def upsert_collection(db, collection_name: str, source_file: Path) -> None:
    print(f"Importing {collection_name:<12} <-  {source_file.as_posix()}")
    documents = load_json_array(source_file)

    operations = []
    for doc in documents:
        if "_id" not in doc:
            raise ValueError(f"Document in {source_file} is missing required _id field")
        operations.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))

    if operations:
        db[collection_name].bulk_write(operations, ordered=False)


def create_indexes(db) -> None:
    print("\nCreating helpful indexes...")
    db.reservations.create_index([("status", ASCENDING), ("check_in", ASCENDING)])
    db.reservations.create_index([("customer_id", ASCENDING)])
    db.reservations.create_index([("destination_city", ASCENDING)])
    db.destinations.create_index([("region", ASCENDING), ("category", ASCENDING)])
    print("Indexes created.")


def print_counts(db) -> None:
    print("\nDone. Document counts:")
    for coll in ("destinations", "flights", "customers", "reservations"):
        print(f"  {coll}: {db[coll].count_documents({})}")


# --------------------------------------------------------------------------- #
# Bulk synthetic data generation (1 000 documents per collection)
# --------------------------------------------------------------------------- #

def _generate_bulk_data(count: int = 1000, seed: int = 42) -> dict[str, list[dict]]:
    """Return a dict with 1000 synthetic records for each collection."""
    rng = random.Random(seed)

    # ------------------------------------------------------------------ #
    # Customers
    # ------------------------------------------------------------------ #
    first_names = [
        "Emma", "Liam", "Olivia", "Noah", "Ava", "Lucas", "Sophia", "Hugo",
        "Mia", "Ethan", "Isabella", "Mason", "Charlotte", "Logan", "Amelia",
        "Elijah", "Harper", "Aiden", "Evelyn", "James", "Abigail", "Oliver",
        "Emily", "Benjamin", "Elizabeth", "Sofia", "Henry", "Avery",
        "Alexander", "Ella", "Michael", "Scarlett", "Daniel", "Victoria",
        "William", "Zoe", "Nathan", "Chloe", "Ryan", "Lily",
    ]
    last_names = [
        "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Petit", "Simon",
        "Laurent", "Leroy", "Moreau", "Garcia", "Martinez", "Smith", "Johnson",
        "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson",
        "Taylor", "Jackson", "White", "Harris", "Clark", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres",
    ]
    countries = [
        "France", "Germany", "United Kingdom", "Spain", "Italy", "Belgium",
        "Netherlands", "Switzerland", "Austria", "Sweden", "United States",
        "Canada", "Australia", "Japan", "Brazil", "Mexico", "Portugal",
        "Poland", "Denmark", "Norway", "South Korea", "India", "Singapore",
    ]
    loyalty_tiers = ["bronze", "silver", "gold", "platinum"]
    tier_weights = [0.50, 0.30, 0.15, 0.05]

    customers: list[dict] = []
    for i in range(count):
        first = rng.choice(first_names)
        last = rng.choice(last_names)
        suffix = rng.randint(1, 99)
        joined_day = date(2019, 1, 1) + timedelta(days=rng.randint(0, 365 * 6))
        customers.append({
            "_id": f"CUST-{2001 + i}",
            "name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{suffix}@example.com",
            "country": rng.choice(countries),
            "loyalty_tier": rng.choices(loyalty_tiers, weights=tier_weights)[0],
            "loyalty_points": rng.randint(0, 120_000),
            "joined": joined_day.isoformat(),
        })

    # ------------------------------------------------------------------ #
    # Destinations  (1 000 unique IDs: DEST-1001 … DEST-2000)
    # ------------------------------------------------------------------ #
    dest_pool = [
        ("Paris", "France", "Europe", "city-break", 220, 4.7, ["culture", "food", "romance"]),
        ("Rome", "Italy", "Europe", "city-break", 190, 4.6, ["history", "food", "art"]),
        ("Barcelona", "Spain", "Europe", "beach", 170, 4.5, ["beach", "nightlife", "architecture"]),
        ("Amsterdam", "Netherlands", "Europe", "city-break", 180, 4.5, ["canals", "culture", "cycling"]),
        ("Vienna", "Austria", "Europe", "city-break", 160, 4.6, ["music", "history", "culture"]),
        ("Prague", "Czech Republic", "Europe", "city-break", 120, 4.7, ["history", "architecture", "beer"]),
        ("Lisbon", "Portugal", "Europe", "city-break", 150, 4.6, ["seafood", "history", "nightlife"]),
        ("Istanbul", "Turkey", "Europe", "city-break", 130, 4.5, ["history", "culture", "food"]),
        ("Athens", "Greece", "Europe", "city-break", 140, 4.4, ["history", "beaches", "food"]),
        ("Madrid", "Spain", "Europe", "city-break", 155, 4.5, ["art", "food", "nightlife"]),
        ("Brussels", "Belgium", "Europe", "city-break", 145, 4.3, ["beer", "chocolate", "culture"]),
        ("Copenhagen", "Denmark", "Europe", "city-break", 210, 4.6, ["design", "food", "culture"]),
        ("Stockholm", "Sweden", "Europe", "city-break", 200, 4.5, ["design", "nature", "culture"]),
        ("Oslo", "Norway", "Europe", "city-break", 230, 4.4, ["fjords", "nature", "culture"]),
        ("Budapest", "Hungary", "Europe", "city-break", 110, 4.6, ["spas", "history", "food"]),
        ("Milan", "Italy", "Europe", "city-break", 180, 4.4, ["fashion", "art", "food"]),
        ("Venice", "Italy", "Europe", "city-break", 210, 4.7, ["canals", "romance", "art"]),
        ("Florence", "Italy", "Europe", "city-break", 175, 4.7, ["art", "renaissance", "food"]),
        ("Seville", "Spain", "Europe", "city-break", 140, 4.5, ["flamenco", "history", "food"]),
        ("Warsaw", "Poland", "Europe", "city-break", 100, 4.3, ["history", "culture", "food"]),
        ("Zurich", "Switzerland", "Europe", "city-break", 280, 4.5, ["nature", "banking", "watches"]),
        ("Helsinki", "Finland", "Europe", "city-break", 190, 4.4, ["design", "nature", "sauna"]),
        ("Dublin", "Ireland", "Europe", "city-break", 170, 4.4, ["pubs", "history", "music"]),
        ("Edinburgh", "United Kingdom", "Europe", "city-break", 160, 4.6, ["history", "whisky", "culture"]),
        ("Kraków", "Poland", "Europe", "city-break", 95, 4.5, ["history", "culture", "food"]),
        ("Tokyo", "Japan", "Asia", "city-break", 240, 4.8, ["food", "technology", "culture"]),
        ("Bangkok", "Thailand", "Asia", "city-break", 110, 4.2, ["food", "temples", "budget"]),
        ("Bali", "Indonesia", "Asia", "resort", 120, 4.7, ["culture", "beach", "yoga"]),
        ("Singapore", "Singapore", "Asia", "city-break", 280, 4.7, ["food", "shopping", "gardens"]),
        ("Hong Kong", "China", "Asia", "city-break", 260, 4.5, ["food", "shopping", "skyline"]),
        ("Seoul", "South Korea", "Asia", "city-break", 200, 4.6, ["food", "technology", "k-pop"]),
        ("Kuala Lumpur", "Malaysia", "Asia", "city-break", 130, 4.3, ["food", "culture", "shopping"]),
        ("Ho Chi Minh City", "Vietnam", "Asia", "city-break", 90, 4.3, ["history", "food", "budget"]),
        ("Hanoi", "Vietnam", "Asia", "city-break", 80, 4.4, ["history", "street-food", "culture"]),
        ("Mumbai", "India", "Asia", "city-break", 110, 4.2, ["bollywood", "food", "culture"]),
        ("New Delhi", "India", "Asia", "city-break", 100, 4.1, ["history", "culture", "food"]),
        ("Maldives", "Maldives", "Asia", "resort", 580, 4.9, ["beach", "luxury", "diving"]),
        ("Phuket", "Thailand", "Asia", "beach", 140, 4.5, ["beach", "diving", "nightlife"]),
        ("Osaka", "Japan", "Asia", "city-break", 200, 4.7, ["food", "culture", "entertainment"]),
        ("Chiang Mai", "Thailand", "Asia", "city-break", 80, 4.5, ["temples", "culture", "trekking"]),
        ("Dubai", "UAE", "Middle East", "city-break", 300, 4.6, ["luxury", "shopping", "beach"]),
        ("Abu Dhabi", "UAE", "Middle East", "city-break", 270, 4.5, ["culture", "luxury", "desert"]),
        ("Doha", "Qatar", "Middle East", "city-break", 250, 4.4, ["culture", "luxury", "desert"]),
        ("Cairo", "Egypt", "Africa", "city-break", 100, 4.2, ["history", "pyramids", "culture"]),
        ("Marrakech", "Morocco", "Africa", "city-break", 120, 4.5, ["culture", "souks", "food"]),
        ("Cape Town", "South Africa", "Africa", "city-break", 150, 4.7, ["nature", "wine", "beaches"]),
        ("Nairobi", "Kenya", "Africa", "adventure", 130, 4.4, ["safari", "wildlife", "nature"]),
        ("Zanzibar", "Tanzania", "Africa", "beach", 160, 4.6, ["beach", "spice", "culture"]),
        ("Sydney", "Australia", "Oceania", "city-break", 290, 4.7, ["beach", "opera", "nature"]),
        ("Melbourne", "Australia", "Oceania", "city-break", 270, 4.6, ["culture", "food", "coffee"]),
        ("Auckland", "New Zealand", "Oceania", "adventure", 220, 4.8, ["nature", "adventure", "film"]),
        ("New York", "United States", "North America", "city-break", 310, 4.4, ["shopping", "broadway", "food"]),
        ("Los Angeles", "United States", "North America", "city-break", 320, 4.4, ["hollywood", "beach", "food"]),
        ("San Francisco", "United States", "North America", "city-break", 350, 4.5, ["tech", "food", "culture"]),
        ("Chicago", "United States", "North America", "city-break", 280, 4.4, ["architecture", "food", "music"]),
        ("Miami", "United States", "North America", "beach", 300, 4.5, ["beach", "nightlife", "art"]),
        ("Cancun", "Mexico", "North America", "resort", 260, 4.3, ["beach", "all-inclusive", "family"]),
        ("Toronto", "Canada", "North America", "city-break", 260, 4.4, ["culture", "food", "diversity"]),
        ("Vancouver", "Canada", "North America", "city-break", 280, 4.6, ["nature", "skiing", "food"]),
        ("Mexico City", "Mexico", "North America", "city-break", 120, 4.3, ["culture", "food", "history"]),
        ("Las Vegas", "United States", "North America", "city-break", 290, 4.2, ["casino", "shows", "nightlife"]),
        ("Rio de Janeiro", "Brazil", "South America", "beach", 160, 4.5, ["carnival", "beaches", "samba"]),
        ("Buenos Aires", "Argentina", "South America", "city-break", 130, 4.5, ["tango", "food", "culture"]),
        ("Bogotá", "Colombia", "South America", "city-break", 100, 4.2, ["culture", "food", "coffee"]),
        ("Lima", "Peru", "South America", "city-break", 110, 4.3, ["food", "history", "culture"]),
        ("Santiago", "Chile", "South America", "city-break", 140, 4.3, ["nature", "wine", "culture"]),
    ]
    categories = ["city-break", "beach", "resort", "adventure"]
    destinations: list[dict] = []
    pool_len = len(dest_pool)
    for i in range(count):
        base = dest_pool[i % pool_len]
        city, country, region, category, base_rate, base_rating, tags = base
        variation = i // pool_len  # 0 for first 65, 1 for next 65, …
        dest_id = f"DEST-{1001 + i}"
        city_label = city if variation == 0 else f"{city} ({variation + 1})"
        rate_jitter = rng.uniform(0.80, 1.20)
        rating_jitter = rng.uniform(-0.2, 0.2)
        destinations.append({
            "_id": dest_id,
            "city": city_label,
            "country": country,
            "region": region,
            "category": rng.choice(categories) if variation > 0 else category,
            "avg_nightly_rate": round(base_rate * rate_jitter),
            "rating": round(min(5.0, max(1.0, base_rating + rating_jitter)), 1),
            "tags": tags,
        })

    # ------------------------------------------------------------------ #
    # Flights  (1 000 records)
    # ------------------------------------------------------------------ #
    airlines_pool = [
        ("Air France", "AF", "CDG"),
        ("British Airways", "BA", "LHR"),
        ("Lufthansa", "LH", "FRA"),
        ("KLM", "KL", "AMS"),
        ("Emirates", "EK", "DXB"),
        ("Qatar Airways", "QR", "DOH"),
        ("Turkish Airlines", "TK", "IST"),
        ("Singapore Airlines", "SQ", "SIN"),
        ("Cathay Pacific", "CX", "HKG"),
        ("Qantas", "QF", "SYD"),
        ("American Airlines", "AA", "JFK"),
        ("Delta Air Lines", "DL", "ATL"),
        ("Air Canada", "AC", "YYZ"),
        ("Iberia", "IB", "MAD"),
        ("TAP Air Portugal", "TP", "LIS"),
        ("Finnair", "AY", "HEL"),
        ("SAS", "SK", "CPH"),
        ("Swiss", "LX", "ZRH"),
        ("Thai Airways", "TG", "BKK"),
        ("Japan Airlines", "JL", "NRT"),
    ]
    airport_city_map = {
        "JFK": "New York", "LHR": "London", "CDG": "Paris", "FRA": "Frankfurt",
        "NRT": "Tokyo", "DXB": "Dubai", "SIN": "Singapore", "SYD": "Sydney",
        "GRU": "Sao Paulo", "LAX": "Los Angeles", "ORD": "Chicago",
        "MIA": "Miami", "AMS": "Amsterdam", "BCN": "Barcelona", "FCO": "Rome",
        "MAD": "Madrid", "IST": "Istanbul", "BKK": "Bangkok", "HKG": "Hong Kong",
        "ICN": "Seoul", "DEL": "New Delhi", "BOM": "Mumbai", "CAI": "Cairo",
        "CPT": "Cape Town", "MEX": "Mexico City", "EZE": "Buenos Aires",
        "AKL": "Auckland", "KUL": "Kuala Lumpur", "DOH": "Doha", "ATL": "Atlanta",
        "YYZ": "Toronto", "LIS": "Lisbon", "HEL": "Helsinki", "CPH": "Copenhagen",
        "ZRH": "Zurich", "VIE": "Vienna", "PRG": "Prague", "BUD": "Budapest",
    }
    all_airports = list(airport_city_map.keys())
    cabins_weighted = (["economy"] * 6) + (["business"] * 3) + (["first"] * 1)
    cabin_multipliers = {"economy": 0.9, "business": 5.0, "first": 12.0}

    flights: list[dict] = []
    used_flight_ids: set[str] = set()
    for i in range(count):
        airline, code, origin = rng.choice(airlines_pool)
        dest_ap = rng.choice([ap for ap in all_airports if ap != origin])
        dest_city = airport_city_map.get(dest_ap, dest_ap)
        cabin = rng.choice(cabins_weighted)
        duration = rng.randint(60, 1200)
        fare = max(50, int(duration * rng.uniform(0.6, 1.4) * cabin_multipliers[cabin]))
        # Guarantee a unique flight ID
        for attempt in range(200):
            num = rng.randint(1, 9999) if attempt < 100 else (10000 + i + attempt)
            flight_id = f"FL-{code}{num:04d}"
            if flight_id not in used_flight_ids:
                break
        used_flight_ids.add(flight_id)
        flights.append({
            "_id": flight_id,
            "airline": airline,
            "origin": origin,
            "destination": dest_ap,
            "dest_city": dest_city,
            "duration_min": duration,
            "cabin": cabin,
            "base_fare": fare,
        })

    # ------------------------------------------------------------------ #
    # Reservations  (1 000 records, cross-referencing the above)
    # ------------------------------------------------------------------ #
    statuses = ["confirmed", "completed", "cancelled", "pending"]
    status_weights = [0.40, 0.35, 0.15, 0.10]
    payment_methods = ["visa", "mastercard", "amex", "paypal", "bank_transfer"]
    currencies = ["EUR", "USD", "GBP"]

    dest_id_list = [d["_id"] for d in destinations]
    dest_city_map = {d["_id"]: d["city"] for d in destinations}
    dest_rate_map = {d["_id"]: d["avg_nightly_rate"] for d in destinations}
    flight_id_list = [f["_id"] for f in flights]
    flight_fare_map = {f["_id"]: f["base_fare"] for f in flights}
    cust_id_list = [c["_id"] for c in customers]
    cust_name_map = {c["_id"]: c["name"] for c in customers}

    reservations: list[dict] = []
    for i in range(count):
        cust_id = rng.choice(cust_id_list)
        dest_id = rng.choice(dest_id_list)
        flight_id = rng.choice(flight_id_list)
        status = rng.choices(statuses, weights=status_weights)[0]
        booking_day = date(2025, 1, 1) + timedelta(days=rng.randint(0, 548))
        check_in = booking_day + timedelta(days=rng.randint(14, 180))
        nights = rng.randint(1, 14)
        travelers = rng.randint(1, 4)
        nightly_rate = dest_rate_map[dest_id]
        room_total = nightly_rate * nights * travelers
        flight_total = flight_fare_map[flight_id] * travelers
        paid = status in ("confirmed", "completed")
        reservations.append({
            "_id": f"RES-{21001 + i}",
            "customer_id": cust_id,
            "customer_name": cust_name_map[cust_id],
            "destination_id": dest_id,
            "destination_city": dest_city_map[dest_id],
            "flight_id": flight_id,
            "status": status,
            "booking_date": booking_day.isoformat(),
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=nights)).isoformat(),
            "nights": nights,
            "travelers": travelers,
            "room_total": room_total,
            "flight_total": flight_total,
            "total_price": room_total + flight_total,
            "currency": rng.choice(currencies),
            "payment": {
                "method": rng.choice(payment_methods),
                "paid": paid,
            },
        })

    return {
        "customers": customers,
        "destinations": destinations,
        "flights": flights,
        "reservations": reservations,
    }


def load_bulk_data(db, count: int = 1000) -> None:
    """Generate and upsert 1 000 synthetic documents into each collection."""
    print(f"\nGenerating {count} synthetic documents per collection...")
    bulk = _generate_bulk_data(count)

    for coll_name, docs in bulk.items():
        print(f"Upserting {coll_name:<12} ({len(docs)} docs)...")
        operations = [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs]
        db[coll_name].bulk_write(operations, ordered=False)

    print("Bulk load complete.")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv_file(repo_root / ".env")

    uri = os.getenv("DOCUMENTDB_URI")
    db_name = "traveldb"

    if not uri:
        print("Missing DOCUMENTDB_URI. Set it in .env or your environment.", file=sys.stderr)
        return 1

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        wait_for_connection(client)
        db = client[db_name]

        fixtures = [
            ("destinations", repo_root / "data" / "destinations.json"),
            ("flights", repo_root / "data" / "flights.json"),
            ("customers", repo_root / "data" / "customers.json"),
            ("reservations", repo_root / "data" / "reservations.json"),
        ]

        for collection, path in fixtures:
            upsert_collection(db, collection, path)

        create_indexes(db)
        load_bulk_data(db, count=1000)
        print_counts(db)
        return 0
    except (OSError, ValueError, PyMongoError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
