# 🚛 Fuel-Efficient Route Optimizer API

A high-performance Django REST API that calculates the **most cost-effective fueling strategy** for a vehicle traveling between two US locations. Uses real truck stop fuel price data (8,151 stations), PostGIS spatial queries, and a provably optimal refueling algorithm.

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    User Request                              │
│         POST /api/route-planner/                             │
│         { start: "New York", end: "Los Angeles" }           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 1: Geocode Locations (Nominatim)                       │
│  "New York, NY" → (40.7128, -74.0060)                       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2: Fetch Route from OSRM  (1 API call)                │
│  → Full route geometry (GeoJSON polyline)                    │
│  → Total distance: 2,790.5 miles                             │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 3: Find Stations Near Route (PostGIS ST_DWithin)       │
│  → Query all stations within 5 miles of route polyline       │
│  → ~150-300 candidate stations for a cross-country trip      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 4: Optimize Fueling Strategy                           │
│  → "To Fill or Not to Fill" Algorithm                        │
│  → Determines EXACTLY where to stop and how much to buy      │
│  → Minimizes total dollar cost across the entire trip         │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Response: Route geometry + Fuel stops + Total cost           │
└──────────────────────────────────────────────────────────────┘
```

## ⚡ Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 15+ with PostGIS extension
- GDAL, GEOS, PROJ libraries (for GeoDjango)

### 1. Clone & Setup Virtual Environment
```bash
cd "project folder"
python3.11 -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Create PostgreSQL Database
```bash
createdb fuel_optimizer_db
psql fuel_optimizer_db -c "CREATE EXTENSION postgis;"
```

### 4. Configure Environment
Edit `.env` with your PostgreSQL credentials (defaults to local macOS setup):
```env
DB_NAME=fuel_optimizer_db
DB_USER=mac
DB_PASSWORD=
DB_HOST=localhost
DB_PORT=5432
```

### 5. Run Migrations
```bash
python manage.py migrate
```

### 6. Import Fuel Station Data
```bash
# Full import with geocoding (~1.9 hours for 6,738 stations)
python manage.py import_fuel_stations

# Quick import without geocoding (for testing)
python manage.py import_fuel_stations --skip-geocoding

# Import only first 50 stations (for testing)
python manage.py import_fuel_stations --limit 50

# Resume geocoding after interruption
python manage.py import_fuel_stations --resume
```

### 7. Start the Server
```bash
python manage.py runserver
```

### 8. Open the Map UI
Navigate to **http://localhost:8000** in your browser.

---

## 📡 API Documentation

### `POST /api/route-planner/`

Calculate the optimal fueling strategy.

**Request:**
```json
{
    "start_location": "New York, NY",
    "end_location": "Los Angeles, CA"
}
```

Both fields accept:
- Place names: `"Chicago, IL"`, `"Houston, TX"`
- Full addresses: `"1600 Pennsylvania Ave, Washington, DC"`
- Raw coordinates: `"40.7128,-74.0060"`

**Response:**
```json
{
    "start_location": {
        "query": "New York, NY",
        "latitude": 40.7128,
        "longitude": -74.006
    },
    "end_location": {
        "query": "Los Angeles, CA",
        "latitude": 34.0522,
        "longitude": -118.2437
    },
    "total_distance_miles": 2790.5,
    "total_duration_hours": 40.2,
    "total_fuel_cost": 872.35,
    "fuel_remaining_gallons": 12.5,
    "num_fuel_stops": 7,
    "route_geometry": { "type": "LineString", "coordinates": [...] },
    "fuel_stops": [
        {
            "name": "PILOT TRAVEL CENTER #1243",
            "address": "I-80, EXIT 310",
            "city": "Clearfield",
            "state": "PA",
            "latitude": 41.0268,
            "longitude": -78.4393,
            "price_per_gallon": 3.149,
            "distance_along_route_miles": 420.0,
            "gallons_to_add": 42.0,
            "cost": 132.26,
            "fuel_level_before": 8.0,
            "fuel_level_after": 50.0
        }
    ],
    "vehicle_specs": {
        "tank_capacity_gallons": 50,
        "mpg": 10,
        "max_range_miles": 500,
        "starts_with_full_tank": true
    },
    "processing_time_ms": 834.2
}
```

---

## 🧮 Algorithm: "To Fill or Not to Fill"

The optimization uses the **"To Fill or Not to Fill" algorithm** (based on Khuller, Malber & Mitchell, 2007), which is **provably optimal** for the fixed-route vehicle refueling problem.

### How It Works

The algorithm processes each fuel station along the route in order, making one of two decisions:

#### Case 1: A cheaper station exists within tank range
→ **Buy only enough fuel to reach it.**

*Rationale:* Why buy expensive fuel now when cheaper fuel is available ahead? This is the "not to fill" case.

#### Case 2: No cheaper station exists within tank range
→ **Fill the tank completely.**

*Rationale:* The current station has the lowest price among all reachable stations. Every gallon purchased here saves money compared to buying at any station within range. This is the "fill" case.

### Why This Is Optimal

The algorithm maintains a key invariant: **fuel is always purchased at the lowest available price**. By:
- Deferring purchases when cheaper fuel is ahead (Case 1)
- Maximizing purchases at local price minima (Case 2)

...it achieves the **global minimum cost** for any given route. This is NOT a naive "fill when empty" or "stop at nearest" approach — it considers the entire price landscape.

### Computational Complexity
- **Time:** O(N²) where N = number of candidate stations (~150-300 per route)
- **Space:** O(N)
- **Practical runtime:** <50ms for cross-country routes

---

## 🗃️ Data Model

### FuelStation
| Field | Type | Description |
|-------|------|-------------|
| `opis_id` | int | OPIS Truckstop ID (indexed) |
| `name` | str | Station name |
| `address` | str | Street/exit address |
| `city` | str | City |
| `state` | str | State code |
| `rack_id` | int | Pricing rack ID |
| `retail_price` | float | $/gallon (indexed) |
| `location` | PointField | PostGIS geography point (SRID 4326) |
| `latitude` | float | Decimal degrees (indexed) |
| `longitude` | float | Decimal degrees (indexed) |

### Spatial Indexing
- PostGIS `PointField` with `geography=True` enables **ST_DWithin** queries using geodetic (great-circle) distance
- Additional composite index on `(latitude, longitude)` for bounding-box pre-filtering
- Price index on `retail_price` for sorting

---

## 🗂️ Project Structure

```
fuel assesment task/
├── manage.py                          # Django CLI
├── fuel_optimizer/                    # Django project
│   ├── settings.py                    # PostGIS, DRF, vehicle constants
│   ├── urls.py                        # Root URL config
│   ├── wsgi.py / asgi.py             # Server entry points
│   └── __init__.py
├── route_planner/                     # Main application
│   ├── models.py                      # FuelStation model with PointField
│   ├── services.py                    # Core: geocoding, OSRM, algorithm
│   ├── views.py                       # DRF API views
│   ├── serializers.py                 # Request/response serializers
│   ├── urls.py                        # App URL routing
│   ├── admin.py                       # Django admin with GIS support
│   ├── apps.py                        # App configuration
│   ├── management/commands/
│   │   └── import_fuel_stations.py    # CSV import + geocoding
│   └── templates/route_planner/
│       └── index.html                 # Leaflet.js map UI
├── fuel-prices-for-be-assessment.csv  # Raw data (8,151 rows)
├── requirements.txt                   # Python dependencies
├── .env                               # Environment configuration
└── README.md                          # This file
```

---

## 🚗 Vehicle Specifications

| Spec | Value |
|------|-------|
| Tank Capacity | 50 gallons |
| Fuel Efficiency | 10 MPG |
| Maximum Range | 500 miles (full tank) |
| Starting Fuel | Full tank (50 gal) |

---

## 📊 Dataset Summary

- **Source:** `fuel-prices.csv`
- **Raw records:** 8,151
- **Unique stations (after dedup):** 6,738
- **Price range:** $2.687 – $6.399/gallon
- **Coverage:** All US states + some Canadian provinces
- **Dedup strategy:** Keep lowest retail price per OPIS Truckstop ID

---

## 🛠️ Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | Django 5.1 + Django REST Framework |
| Database | PostgreSQL 15 + PostGIS 3.6 |
| Spatial | GeoDjango (ST_DWithin, PointField) |
| Routing | OSRM Public API (1 call per request) |
| Geocoding | Nominatim via geopy (import-time only) |
| Frontend | Leaflet.js + CartoDB Dark Matter tiles |
| Algorithm | "To Fill or Not to Fill" (O(N²), optimal) |
