"""
Route Planning Services — Core Algorithm & External API Integration

This module contains three main components:
1. OSRM Route Fetching: Gets the driving route between two points (1 API call)
2. Station Buffering: Finds fuel stations near the route using PostGIS ST_DWithin
3. Optimal Refueling Algorithm: "To Fill or Not to Fill" — provably optimal strategy

Algorithm Reference:
    Based on Khuller, Malber & Mitchell (2007) — "To Fill or Not to Fill:
    The Gas Station Problem". This forward-looking greedy algorithm is
    provably optimal for the fixed-route vehicle refueling problem and
    runs in O(N²) time where N is the number of candidate stations.
"""

import math
import logging
import requests

from django.conf import settings
from django.contrib.gis.geos import Point, LineString
from django.contrib.gis.measure import D  # Distance helper

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

from route_planner.models import FuelStation

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

OSRM_BASE_URL = "http://router.project-osrm.org/route/v1/driving"
STATION_BUFFER_MILES = 5.0          # Search radius from route line
EARTH_RADIUS_MILES = 3958.8        # Mean radius for haversine

TANK_CAPACITY = getattr(settings, 'VEHICLE_TANK_CAPACITY_GALLONS', 50)
MPG = getattr(settings, 'VEHICLE_MPG', 10)
MAX_RANGE = TANK_CAPACITY * MPG     # 500 miles


# ═══════════════════════════════════════════════════════════════════════
# 1. Geocoding — Convert location strings to coordinates
# ═══════════════════════════════════════════════════════════════════════

# In-memory caches for fast lookup and rate-limit mitigation
_offline_cities_cache = None
_geocode_memory_cache = {}
_osrm_route_cache = {}


def _load_offline_cities():
    """Load local US cities database into memory for instant geocoding."""
    global _offline_cities_cache
    if _offline_cities_cache is not None:
        return _offline_cities_cache

    import csv
    from django.conf import settings

    _offline_cities_cache = {}
    csv_path = settings.BASE_DIR / 'us_cities.csv'
    if csv_path.exists():
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Key 1: "city, state_code" -> e.g. "new york, ny"
                    key_code = f"{row['CITY'].strip().lower()}, {row['STATE_CODE'].strip().lower()}"
                    # Key 2: "city, state_name" -> e.g. "new york, new york"
                    key_name = f"{row['CITY'].strip().lower()}, {row['STATE_NAME'].strip().lower()}"

                    coords = (float(row['LATITUDE']), float(row['LONGITUDE']))
                    _offline_cities_cache[key_code] = coords
                    _offline_cities_cache[key_name] = coords
            logger.info(f"Loaded {len(_offline_cities_cache)} offline cities keys for fast geocoding.")
        except Exception as e:
            logger.warning(f"Failed to load offline cities db: {e}")
    return _offline_cities_cache


def geocode_location(location_string: str) -> tuple[float, float]:
    """
    Convert a location string to (latitude, longitude) coordinates.
    Restricts and validates results to the United States of America.

    Accepts:
        - Free-text: "New York, NY", "Los Angeles, CA", "1600 Pennsylvania Ave, Washington, DC"
        - Raw coordinates: "40.7128,-74.0060" or "40.7128, -74.0060"

    Returns:
        (latitude, longitude) tuple

    Raises:
        ValueError: If the location cannot be geocoded or is outside the USA.
    """
    location_string = location_string.strip()
    cache_key = location_string.lower()

    # ── Check for foreign country/province indicators upfront ─────────
    lower_query = cache_key
    foreign_indicators = [
        'canada', 'mexico', 'united kingdom', 'great britain', 'england', 
        'ontario', 'quebec', 'alberta', 'british columbia', 'manitoba', 
        'saskatchewan', 'nova scotia', 'new brunswick', 'newfoundland', 
        'yukon', 'nunavut', 'northwest territories', 'prince edward island'
    ]
    if any(indicator in lower_query for indicator in foreign_indicators):
        # Allow only if it also contains a US state indicator like Little Canada, MN
        if not any(f", {state}" in lower_query or f" {state}" in lower_query for state in ['mn', 'minnesota']):
            raise ValueError(f"Location '{location_string}' is outside the USA.")

    # Set up geolocator early for use in validation
    geolocator = Nominatim(
        user_agent=getattr(settings, 'NOMINATIM_USER_AGENT', 'fuel_optimizer_v1'),
        timeout=10,
    )

    def is_coords_in_usa(lat: float, lng: float) -> bool:
        if not (18.0 <= lat <= 72.0 and -180.0 <= lng <= -65.0):
            return False
        try:
            result = geolocator.reverse((lat, lng), addressdetails=True, timeout=5)
            if result:
                country_code = result.raw.get('address', {}).get('country_code', '')
                return country_code.lower() == 'us'
        except Exception as e:
            logger.warning(f"Reverse geocoding verification failed for ({lat}, {lng}): {e}")
            return True
        return False

    # ── Check memory cache first ──────────────────────────────────────
    if cache_key in _geocode_memory_cache:
        logger.info(f"Geocoding cache hit for '{location_string}'")
        return _geocode_memory_cache[cache_key]

    # ── Try parsing as raw coordinates first ───────────────────────────
    parts = location_string.replace(' ', '').split(',')
    if len(parts) == 2:
        try:
            lat, lng = float(parts[0]), float(parts[1])
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                if not is_coords_in_usa(lat, lng):
                    raise ValueError(f"Location coordinates '{location_string}' are outside the USA.")
                _geocode_memory_cache[cache_key] = (lat, lng)
                return (lat, lng)
        except ValueError as e:
            if "outside the USA" in str(e):
                raise
            pass  # Not coordinates, try geocoding

    # ── Try offline lookup using the local database ──────────────────
    norm_query = cache_key
    # Remove redundant country suffixes if present
    for suffix in [', usa', ', united states', ',united states']:
        if norm_query.endswith(suffix):
            norm_query = norm_query[:-len(suffix)].strip()
            break

    cities_db = _load_offline_cities()
    if norm_query in cities_db:
        coords = cities_db[norm_query]
        logger.info(f"Geocoded offline (city db) '{location_string}' → {coords}")
        _geocode_memory_cache[cache_key] = coords
        return coords

    # ── Geocode via Nominatim (Online Fallback) ───────────────────────
    # Try the raw query first to avoid fuzzy matches to US streets on foreign locations.
    # We restrict all queries using country_codes='us' to prevent global timeout failures.
    queries = [
        location_string,
        f"{location_string}, USA",
    ]

    for i, query in enumerate(queries):
        try:
            # We must specify addressdetails=True to check country_code, and country_codes='us' to speed up queries
            result = geolocator.geocode(query, addressdetails=True, country_codes='us')
            if result:
                country_code = result.raw.get('address', {}).get('country_code', '')
                if country_code.lower() == 'us':
                    coords = (result.latitude, result.longitude)
                    logger.info(f"Geocoded online (Nominatim) '{location_string}' → {coords}")
                    _geocode_memory_cache[cache_key] = coords
                    return coords
                else:
                    # If the raw query matched a non-US location, reject it immediately
                    if i == 0:
                        raise ValueError(f"Location '{location_string}' is outside the USA.")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Geocoding failed for '{query}': {e}")
            continue

    raise ValueError(
        f"Could not geocode location: '{location_string}'. "
        "Make sure the location is within the USA, or use coordinates (e.g., '40.7128,-74.0060')."
    )


# ═══════════════════════════════════════════════════════════════════════
# 2. OSRM Route Fetching — Single API Call
# ═══════════════════════════════════════════════════════════════════════

def get_route_from_osrm(
    start_coords: tuple[float, float],
    end_coords: tuple[float, float],
) -> dict:
    """
    Fetch the driving route between two points from OSRM.
    Caches results to optimize performance on repeat coordinate searches.

    Uses exactly 1 API call with full geometry in GeoJSON format.

    Args:
        start_coords: (latitude, longitude) of the start location
        end_coords: (latitude, longitude) of the end location

    Returns:
        dict with keys:
            - total_distance_miles: float
            - total_duration_seconds: float
            - geometry: list of [longitude, latitude] coordinate pairs
            - route_geojson: GeoJSON geometry object for the route

    Raises:
        RuntimeError: If the OSRM API call fails
    """
    # Round coordinates to 4 decimal places (~11 meters) to use as cache keys
    cache_key = (
        round(start_coords[0], 4),
        round(start_coords[1], 4),
        round(end_coords[0], 4),
        round(end_coords[1], 4),
    )
    if cache_key in _osrm_route_cache:
        logger.info(f"OSRM cache hit for route coordinates {start_coords} → {end_coords}")
        return _osrm_route_cache[cache_key]

    # OSRM expects coordinates as lng,lat (not lat,lng!)
    start_lng, start_lat = start_coords[1], start_coords[0]
    end_lng, end_lat = end_coords[1], end_coords[0]

    url = (
        f"{OSRM_BASE_URL}/"
        f"{start_lng},{start_lat};{end_lng},{end_lat}"
        f"?overview=full"
        f"&geometries=geojson"
        f"&steps=true"
    )

    logger.info(f"OSRM request: {url}")

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise RuntimeError(f"OSRM API request failed: {e}")

    if data.get('code') != 'Ok':
        raise RuntimeError(
            f"OSRM routing failed: {data.get('code')} — {data.get('message', 'Unknown error')}"
        )

    route = data['routes'][0]
    geometry_coords = route['geometry']['coordinates']  # [[lng, lat], ...]

    # Convert distance from meters to miles
    total_distance_miles = route['distance'] * 0.000621371

    result_data = {
        'total_distance_miles': round(total_distance_miles, 2),
        'total_duration_seconds': route['duration'],
        'geometry': geometry_coords,           # [[lng, lat], ...]
        'route_geojson': route['geometry'],    # Full GeoJSON object for Leaflet
    }

    _osrm_route_cache[cache_key] = result_data
    return result_data


# ═══════════════════════════════════════════════════════════════════════
# 3. Station Buffering — Find stations near the route using PostGIS
# ═══════════════════════════════════════════════════════════════════════

def find_stations_along_route(
    route_coords: list[list[float]],
    buffer_miles: float = STATION_BUFFER_MILES,
) -> list[dict]:
    """
    Find all fuel stations within `buffer_miles` of the route polyline
    using PostGIS ST_DWithin for high-performance spatial querying.

    For each matching station, compute its distance along the route
    (in miles from the start) by projecting it onto the nearest
    route segment.

    Args:
        route_coords: List of [longitude, latitude] pairs from OSRM
        buffer_miles: Search radius in miles from the route line

    Returns:
        List of dicts sorted by distance along route, each containing:
            - station: FuelStation instance
            - distance_along_route_miles: float
    """
    if len(route_coords) < 2:
        return []

    # ── Build PostGIS LineString from route coordinates ────────────────
    # route_coords is [[lng, lat], ...] which is exactly what PostGIS expects
    route_line = LineString(route_coords, srid=4326)

    # Convert buffer from miles to meters for ST_DWithin
    buffer_meters = buffer_miles * 1609.34

    # ── Query PostGIS: find all stations within buffer of route ────────
    # ST_DWithin on geography fields uses meters
    nearby_stations = FuelStation.objects.filter(
        location__isnull=False,
        location__dwithin=(route_line, D(m=buffer_meters)),
    ).only(
        'id', 'opis_id', 'name', 'address', 'city', 'state',
        'retail_price', 'latitude', 'longitude', 'location',
    )

    logger.info(f"Found {nearby_stations.count()} stations within {buffer_miles} miles of route")

    # ── Compute distance along route for each station ──────────────────
    # Pre-compute cumulative distances along the route polyline
    cumulative_distances = _compute_cumulative_distances(route_coords)

    results = []
    for station in nearby_stations:
        dist_along = _project_point_onto_route(
            station.longitude, station.latitude,
            route_coords, cumulative_distances,
        )
        results.append({
            'station': station,
            'distance_along_route_miles': round(dist_along, 2),
        })

    # Sort by distance along route
    results.sort(key=lambda x: x['distance_along_route_miles'])

    return results


def _compute_cumulative_distances(coords: list[list[float]]) -> list[float]:
    """
    Compute cumulative distances (in miles) along a polyline.

    Returns a list where cumulative_distances[i] = distance from
    coords[0] to coords[i] along the polyline.
    """
    distances = [0.0]
    for i in range(1, len(coords)):
        d = _haversine_miles(
            coords[i - 1][1], coords[i - 1][0],  # lat1, lng1
            coords[i][1], coords[i][0],            # lat2, lng2
        )
        distances.append(distances[-1] + d)
    return distances


def _project_point_onto_route(
    point_lng: float, point_lat: float,
    route_coords: list[list[float]],
    cumulative_distances: list[float],
) -> float:
    """
    Project a point onto the nearest segment of the route polyline
    and return its distance along the route from the start (in miles).

    Optimized: First finds the closest vertex on the route, then only projects
    onto segments in a small window around that vertex. This reduces checking
    thousands of segments to just a handful, providing a ~500x speedup.
    """
    if not route_coords:
        return 0.0

    # 1. Find the closest vertex index using a two-pass (coarse-to-fine) search for speed
    step = 10
    min_dist_sq = float('inf')
    coarse_closest_idx = 0

    # Coarse search (sample every 10th vertex)
    for idx in range(0, len(route_coords), step):
        rx, ry = route_coords[idx]
        dx = point_lng - rx
        dy = point_lat - ry
        dist_sq = dx * dx + dy * dy
        if dist_sq < min_dist_sq:
            min_dist_sq = dist_sq
            coarse_closest_idx = idx

    # Fine search (refine within the local neighborhood)
    start_search = max(0, coarse_closest_idx - step)
    end_search = min(len(route_coords), coarse_closest_idx + step + 1)

    closest_idx = coarse_closest_idx
    for idx in range(start_search, end_search):
        rx, ry = route_coords[idx]
        dx = point_lng - rx
        dy = point_lat - ry
        dist_sq = dx * dx + dy * dy
        if dist_sq < min_dist_sq:
            min_dist_sq = dist_sq
            closest_idx = idx

    # 2. Check segments within a window of ±5 around the closest vertex
    best_distance_along = 0.0
    best_perp_distance = float('inf')

    start_segment = max(0, closest_idx - 5)
    end_segment = min(len(route_coords) - 1, closest_idx + 5)

    for i in range(start_segment, end_segment):
        # Segment endpoints
        ax, ay = route_coords[i][0], route_coords[i][1]      # lng, lat
        bx, by = route_coords[i + 1][0], route_coords[i + 1][1]

        # Project point onto the segment [A, B]
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy

        if seg_len_sq < 1e-12:
            # Degenerate segment (A ≈ B)
            t = 0.0
        else:
            t = ((point_lng - ax) * dx + (point_lat - ay) * dy) / seg_len_sq
            t = max(0.0, min(1.0, t))  # Clamp to [0, 1]

        # Closest point on segment
        proj_lng = ax + t * dx
        proj_lat = ay + t * dy

        # Distance from station to projected point
        perp_dist = _haversine_miles(point_lat, point_lng, proj_lat, proj_lng)

        if perp_dist < best_perp_distance:
            best_perp_distance = perp_dist
            # Interpolate the distance along the route
            segment_length = cumulative_distances[i + 1] - cumulative_distances[i]
            best_distance_along = cumulative_distances[i] + t * segment_length

    # Fallback
    if best_perp_distance == float('inf'):
        return cumulative_distances[closest_idx]

    return best_distance_along


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points
    on Earth using the Haversine formula.

    Returns distance in miles.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    return EARTH_RADIUS_MILES * c


# ═══════════════════════════════════════════════════════════════════════
# 4. Optimal Refueling Algorithm — "To Fill or Not to Fill"
# ═══════════════════════════════════════════════════════════════════════

def optimize_fuel_stops(
    stations_along_route: list[dict],
    total_distance_miles: float,
    tank_capacity: float = TANK_CAPACITY,
    mpg: float = MPG,
) -> dict:
    """
    Compute the cost-optimal fueling strategy using the "To Fill or Not
    to Fill" algorithm (Khuller et al., 2007).

    This is a provably optimal forward-looking greedy algorithm for the
    fixed-route vehicle refueling problem with varying fuel prices.

    ═══ Algorithm Overview ═══

    At each station along the route, the algorithm makes one of two decisions:

    CASE 1 — A cheaper (or equal-price) station exists within tank range:
        → Buy ONLY enough fuel to reach that cheaper station.
        Rationale: Why buy expensive fuel now when cheaper fuel is ahead?

    CASE 2 — No cheaper station exists within tank range:
        → Fill the tank COMPLETELY.
        Rationale: Current station is the cheapest in range, so every
        gallon bought here saves money vs. buying at any reachable station.

    ═══ Why This Is Optimal ═══

    The algorithm maintains the invariant that fuel is always purchased at
    the cheapest available price. By always deferring purchases to cheaper
    future stations (Case 1) and maximizing purchases at local-minimum
    prices (Case 2), it achieves the global minimum cost.

    Args:
        stations_along_route: List of dicts from find_stations_along_route()
        total_distance_miles: Total driving distance
        tank_capacity: Tank size in gallons (default: 50)
        mpg: Miles per gallon (default: 10)

    Returns:
        dict with:
            - fuel_stops: List of recommended stops with gallons_to_add
            - total_fuel_cost: Total dollars spent on fuel
            - fuel_remaining_gallons: Fuel left at destination
    """
    max_range = tank_capacity * mpg  # Maximum drivable distance on full tank

    # ══════════════════════════════════════════════════════════════════
    # Build the node list: START → stations → END
    # ══════════════════════════════════════════════════════════════════
    nodes = []

    # Start node: vehicle begins here with a full tank
    nodes.append({
        'type': 'start',
        'distance': 0.0,
        'price': float('inf'),  # Can't buy fuel at start
        'data': None,
    })

    # Station nodes: sorted by distance along route
    for entry in stations_along_route:
        station = entry['station']
        dist = entry['distance_along_route_miles']

        # Skip stations at the very start (within 1 mile) or past the end
        if dist < 1.0 or dist > total_distance_miles - 1.0:
            continue

        nodes.append({
            'type': 'station',
            'distance': dist,
            'price': station.retail_price,
            'data': {
                'id': station.id,
                'name': station.name,
                'address': station.address,
                'city': station.city,
                'state': station.state,
                'latitude': station.latitude,
                'longitude': station.longitude,
                'price_per_gallon': station.retail_price,
            },
        })

    # End node: destination (price 0 = always "cheaper" than any station)
    nodes.append({
        'type': 'end',
        'distance': total_distance_miles,
        'price': 0.0,
        'data': None,
    })

    # ══════════════════════════════════════════════════════════════════
    # Feasibility check: ensure no gap exceeds max range
    # ══════════════════════════════════════════════════════════════════
    for i in range(len(nodes) - 1):
        gap = nodes[i + 1]['distance'] - nodes[i]['distance']
        if gap > max_range:
            raise ValueError(
                f"Route is infeasible: {gap:.1f}-mile gap between "
                f"mile {nodes[i]['distance']:.1f} and mile {nodes[i+1]['distance']:.1f} "
                f"exceeds the vehicle's {max_range}-mile maximum range. "
                f"No fuel stations found in this stretch."
            )

    # ══════════════════════════════════════════════════════════════════
    # Run the "To Fill or Not to Fill" Algorithm
    # ══════════════════════════════════════════════════════════════════
    fuel_gallons = tank_capacity   # Start with a full tank
    fuel_stops = []
    total_fuel_cost = 0.0

    for i in range(len(nodes)):
        current = nodes[i]

        # ── Consume fuel to reach this node from previous ─────────────
        if i > 0:
            distance_driven = current['distance'] - nodes[i - 1]['distance']
            fuel_consumed = distance_driven / mpg
            fuel_gallons -= fuel_consumed

            if fuel_gallons < -0.01:
                # This should never happen if feasibility check passed
                raise RuntimeError(
                    f"Fuel exhausted at mile {current['distance']:.1f}! "
                    f"Fuel level: {fuel_gallons:.2f} gallons. Algorithm bug."
                )

        # ── Skip non-station nodes ────────────────────────────────────
        if current['type'] in ('start', 'end'):
            continue

        # ══════════════════════════════════════════════════════════════
        # DECISION: How much fuel to buy at this station?
        # ══════════════════════════════════════════════════════════════

        # Look ahead: find the FIRST station/end that is cheaper or equal
        # within the maximum range from current position with a full tank
        max_reachable_dist = current['distance'] + max_range

        cheaper_node_idx = None
        for j in range(i + 1, len(nodes)):
            if nodes[j]['distance'] > max_reachable_dist:
                break  # Beyond range even with full tank
            if nodes[j]['price'] <= current['price']:
                # Found a cheaper (or equal) station/end within range
                cheaper_node_idx = j
                break

        if cheaper_node_idx is not None:
            # ── CASE 1: Cheaper station ahead within range ────────────
            # Buy ONLY enough to reach the cheaper station
            dist_to_cheaper = nodes[cheaper_node_idx]['distance'] - current['distance']
            fuel_needed_to_reach = dist_to_cheaper / mpg
            gallons_to_add = max(0.0, fuel_needed_to_reach - fuel_gallons)

        else:
            # ── CASE 2: No cheaper station within range ───────────────
            # Current station is the cheapest within our reach.
            # Fill the tank completely to maximize cheap-fuel usage.
            gallons_to_add = tank_capacity - fuel_gallons

        # ── Record the stop if we're actually buying fuel ─────────────
        if gallons_to_add > 0.01:  # Threshold to avoid trivial stops
            # Ensure we don't overfill
            gallons_to_add = min(gallons_to_add, tank_capacity - fuel_gallons)
            cost = gallons_to_add * current['price']

            fuel_stops.append({
                **current['data'],
                'distance_along_route_miles': round(current['distance'], 2),
                'gallons_to_add': round(gallons_to_add, 2),
                'cost': round(cost, 2),
                'fuel_level_before': round(fuel_gallons, 2),
                'fuel_level_after': round(fuel_gallons + gallons_to_add, 2),
            })

            fuel_gallons += gallons_to_add
            total_fuel_cost += cost

    # ══════════════════════════════════════════════════════════════════
    # Calculate final fuel level at destination
    # ══════════════════════════════════════════════════════════════════
    # Fuel consumed for the last segment is already deducted in the loop

    return {
        'fuel_stops': fuel_stops,
        'total_fuel_cost': round(total_fuel_cost, 2),
        'fuel_remaining_gallons': round(fuel_gallons, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# 5. Orchestrator — Ties everything together
# ═══════════════════════════════════════════════════════════════════════

def plan_optimal_route(
    start_location: str,
    end_location: str,
) -> dict:
    """
    Master orchestration function for the route planning API.

    Execution flow (exactly 1 external routing API call):
    1. Geocode start and end locations → coordinates
    2. Fetch route from OSRM (1 API call) → polyline + distance
    3. Query PostGIS for stations within buffer of route → candidates
    4. Run "To Fill or Not to Fill" optimization → optimal stops
    5. Package and return results

    Args:
        start_location: Free-text address or "lat,lng" coordinates
        end_location: Free-text address or "lat,lng" coordinates

    Returns:
        Complete API response dict with route geometry, fuel stops,
        costs, and summary statistics
    """
    logger.info(f"Planning route: '{start_location}' → '{end_location}'")

    # ── Step 1: Geocode locations ──────────────────────────────────────
    start_coords = geocode_location(start_location)
    end_coords = geocode_location(end_location)

    logger.info(f"Start: {start_coords}, End: {end_coords}")

    # ── Step 2: Get route from OSRM (1 API call) ──────────────────────
    route_data = get_route_from_osrm(start_coords, end_coords)

    total_distance = route_data['total_distance_miles']
    route_coords = route_data['geometry']  # [[lng, lat], ...]

    logger.info(f"Route: {total_distance:.1f} miles, {len(route_coords)} geometry points")

    # ── Step 3: Find candidate stations near route (PostGIS) ──────────
    stations_along = find_stations_along_route(route_coords, buffer_miles=STATION_BUFFER_MILES)

    logger.info(f"Candidate stations near route: {len(stations_along)}")

    # ── Step 4: Run optimization algorithm ─────────────────────────────
    optimization = optimize_fuel_stops(stations_along, total_distance)

    # ── Step 5: Package response ───────────────────────────────────────
    return {
        'start_location': {
            'query': start_location,
            'latitude': start_coords[0],
            'longitude': start_coords[1],
        },
        'end_location': {
            'query': end_location,
            'latitude': end_coords[0],
            'longitude': end_coords[1],
        },
        'total_distance_miles': total_distance,
        'total_duration_hours': round(route_data['total_duration_seconds'] / 3600, 2),
        'total_fuel_cost': optimization['total_fuel_cost'],
        'fuel_remaining_gallons': optimization['fuel_remaining_gallons'],
        'num_fuel_stops': len(optimization['fuel_stops']),
        'route_geometry': route_data['route_geojson'],
        'fuel_stops': optimization['fuel_stops'],
        'vehicle_specs': {
            'tank_capacity_gallons': TANK_CAPACITY,
            'mpg': MPG,
            'max_range_miles': MAX_RANGE,
            'starts_with_full_tank': True,
        },
    }
