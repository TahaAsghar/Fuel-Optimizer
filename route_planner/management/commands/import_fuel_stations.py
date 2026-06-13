"""
Management Command: import_fuel_stations

Reads the CSV dataset, deduplicates by OPIS ID (keeping cheapest price),
geocodes each station address via Nominatim, and saves to the database.

Usage:
    python manage.py import_fuel_stations                     # Full import
    python manage.py import_fuel_stations --limit 50          # Import first 50
    python manage.py import_fuel_stations --skip-geocoding    # Import without geocoding
    python manage.py import_fuel_stations --resume            # Resume geocoding only

Features:
    - Resume-safe: skips already-geocoded stations on re-run
    - Rate-limited: 1 request per 1.1 seconds (Nominatim policy)
    - Fallback: tries City/State if full address geocoding fails
    - Batch commits: saves every 50 records for crash safety
    - Progress bar via tqdm
"""

import csv
import time
from pathlib import Path
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.contrib.gis.geos import Point
from django.conf import settings

from route_planner.models import FuelStation

# Conditional imports — these are installed via requirements.txt
try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    GEOPY_AVAILABLE = True
except ImportError:
    GEOPY_AVAILABLE = False

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False


class Command(BaseCommand):
    help = (
        "Import fuel station data from the CSV file, deduplicate by OPIS ID "
        "(keeping cheapest price), geocode addresses, and save to PostGIS database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv-path',
            type=str,
            default=str(Path(settings.BASE_DIR) / 'fuel-prices-for-be-assessment.csv'),
            help='Path to the CSV file (default: project root)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit the number of stations to import (for testing)',
        )
        parser.add_argument(
            '--skip-geocoding',
            action='store_true',
            help='Import CSV data without geocoding (coordinates will be NULL)',
        )
        parser.add_argument(
            '--resume',
            action='store_true',
            help='Only geocode stations that are missing coordinates (skip CSV import)',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=50,
            help='Number of records to commit in each batch (default: 50)',
        )

    def handle(self, *args, **options):
        csv_path = options['csv_path']
        limit = options['limit']
        skip_geocoding = options['skip_geocoding']
        resume_only = options['resume']
        batch_size = options['batch_size']

        # ──────────────────────────────────────────────
        # Phase 1: CSV Import (unless --resume)
        # ──────────────────────────────────────────────
        if not resume_only:
            self.stdout.write(self.style.MIGRATE_HEADING("\n═══ Phase 1: Importing CSV Data ═══"))
            self._import_csv(csv_path, limit)

        # ──────────────────────────────────────────────
        # Phase 2: Geocoding (unless --skip-geocoding)
        # ──────────────────────────────────────────────
        if not skip_geocoding:
            self.stdout.write(self.style.MIGRATE_HEADING("\n═══ Phase 2: Geocoding Stations ═══"))
            self._geocode_stations(batch_size, limit)

        # ──────────────────────────────────────────────
        # Summary
        # ──────────────────────────────────────────────
        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.filter(location__isnull=False).count()
        self.stdout.write(self.style.SUCCESS(
            f"\n✅ Done! {total} stations in database, {geocoded} geocoded "
            f"({geocoded/total*100:.1f}%)" if total > 0 else "\n⚠️ No stations in database."
        ))

    def _import_csv(self, csv_path: str, limit: int | None):
        """
        Read the CSV, deduplicate by OPIS ID (keep cheapest price per ID),
        and bulk-insert into the database.
        """
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        self.stdout.write(f"  Reading: {path.name}")

        # ── Read and deduplicate ──────────────────────────────────────
        # Group rows by OPIS ID, keep the one with the lowest Retail Price
        rows_by_id = defaultdict(list)

        with open(path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                opis_id = row['OPIS Truckstop ID'].strip()
                rows_by_id[opis_id].append(row)

        # For each OPIS ID, pick the row with the lowest retail price
        deduplicated = []
        for opis_id, rows in rows_by_id.items():
            cheapest = min(rows, key=lambda r: float(r['Retail Price']))
            deduplicated.append(cheapest)

        self.stdout.write(
            f"  Raw rows: {sum(len(v) for v in rows_by_id.values())} | "
            f"Unique OPIS IDs: {len(deduplicated)} | "
            f"Duplicates removed: {sum(len(v) for v in rows_by_id.values()) - len(deduplicated)}"
        )

        # Apply limit if specified
        if limit:
            deduplicated = deduplicated[:limit]
            self.stdout.write(f"  Limiting to first {limit} stations")

        # ── Clear existing data and bulk insert ───────────────────────
        deleted_count, _ = FuelStation.objects.all().delete()
        if deleted_count > 0:
            self.stdout.write(f"  Cleared {deleted_count} existing records")

        stations = []
        for row in deduplicated:
            stations.append(FuelStation(
                opis_id=int(row['OPIS Truckstop ID'].strip()),
                name=row['Truckstop Name'].strip(),
                address=row['Address'].strip(),
                city=row['City'].strip(),
                state=row['State'].strip(),
                rack_id=int(row['Rack ID'].strip()),
                retail_price=float(row['Retail Price'].strip()),
            ))

        FuelStation.objects.bulk_create(stations, batch_size=500)
        self.stdout.write(self.style.SUCCESS(f"  ✓ Imported {len(stations)} stations"))

    def _geocode_stations(self, batch_size: int, limit: int | None):
        """
        Geocode all stations missing coordinates using a hybrid approach:
        1. Attempt offline geocoding using a local US cities database (us_cities.csv).
        2. For remaining unmatched stations, attempt online geocoding via Nominatim.
        """
        # ── Setup/Download Local City Database ───────────────────────
        local_db_path = Path(settings.BASE_DIR) / 'us_cities.csv'
        if not local_db_path.exists():
            self.stdout.write("  Downloading offline US cities database...")
            try:
                import requests
                url = "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv"
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                local_db_path.write_bytes(response.content)
                self.stdout.write(self.style.SUCCESS("  ✓ Offline city database downloaded successfully."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠️ Could not download offline database: {e}. "
                    "Will rely fully on online Nominatim geocoding (this may be very slow)."
                ))

        cities_db = {}
        if local_db_path.exists():
            self.stdout.write("  Loading offline city database...")
            try:
                with open(local_db_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        key = (row['CITY'].strip().lower(), row['STATE_CODE'].strip().upper())
                        cities_db[key] = (float(row['LATITUDE']), float(row['LONGITUDE']))
                self.stdout.write(self.style.SUCCESS(f"  ✓ Loaded {len(cities_db)} cities for offline geocoding."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  ⚠️ Error parsing local database: {e}"))

        # Fetch stations missing coordinates
        pending = FuelStation.objects.filter(location__isnull=True)
        if limit:
            pending = pending[:limit]

        pending_list = list(pending)
        total_pending = len(pending_list)

        if total_pending == 0:
            self.stdout.write(self.style.SUCCESS("  ✓ All stations already geocoded!"))
            return

        self.stdout.write(f"  Stations to geocode: {total_pending}")

        # ── Phase 2a: Offline Geocoding ──────────────────────────────
        offline_success = 0
        online_candidates = []
        batch_buffer = []

        self.stdout.write("  Starting offline geocoding pass...")
        for station in pending_list:
            key = (station.city.strip().lower(), station.state.strip().upper())
            if key in cities_db:
                lat, lng = cities_db[key]
                station.latitude = lat
                station.longitude = lng
                station.location = Point(lng, lat, srid=4326)
                offline_success += 1
                batch_buffer.append(station)

                if len(batch_buffer) >= batch_size:
                    FuelStation.objects.bulk_update(
                        batch_buffer,
                        ['latitude', 'longitude', 'location'],
                        batch_size=batch_size,
                    )
                    batch_buffer = []
            else:
                online_candidates.append(station)

        if batch_buffer:
            FuelStation.objects.bulk_update(
                batch_buffer,
                ['latitude', 'longitude', 'location'],
                batch_size=batch_size,
            )

        self.stdout.write(self.style.SUCCESS(
            f"  ✓ Offline geocoding finished: {offline_success} stations geocoded."
        ))

        # ── Phase 2b: Online Geocoding for Unmatched ──────────────────
        total_online = len(online_candidates)
        if total_online == 0:
            self.stdout.write(self.style.SUCCESS("  ✓ No stations remaining for online geocoding."))
            return

        if not GEOPY_AVAILABLE:
            self.stdout.write(self.style.WARNING(
                f"  ⚠️ {total_online} stations require online geocoding, but geopy is not installed. "
                "Run: pip install geopy"
            ))
            return

        self.stdout.write(f"  {total_online} stations remain. Querying Nominatim...")
        self.stdout.write(f"  Estimated time: ~{total_online * 1.1 / 60:.1f} minutes")

        # Set up online geocoder
        geolocator = Nominatim(
            user_agent=settings.NOMINATIM_USER_AGENT,
            timeout=10,
        )
        geocode = RateLimiter(
            geolocator.geocode,
            min_delay_seconds=1.1,
            max_retries=3,
            error_wait_seconds=5.0,
            return_value_on_exception=None,
        )

        online_success_count = 0
        fallback_count = 0
        fail_count = 0
        batch_buffer = []

        iterator = tqdm(online_candidates, desc="  Online Geocoding") if TQDM_AVAILABLE else online_candidates

        for station in iterator:
            location = None

            # Attempt 1: Full address
            full_address = f"{station.address}, {station.city}, {station.state}, USA"
            try:
                location = geocode(full_address)
            except (GeocoderTimedOut, GeocoderServiceError) as e:
                self.stdout.write(self.style.WARNING(f"  ⚠ Timeout for: {full_address} — {e}"))

            # Attempt 2: Fallback to City, State
            if location is None:
                city_state = f"{station.city}, {station.state}, USA"
                try:
                    location = geocode(city_state)
                    if location:
                        fallback_count += 1
                except (GeocoderTimedOut, GeocoderServiceError):
                    pass

            # Save coordinates
            if location:
                station.latitude = location.latitude
                station.longitude = location.longitude
                station.location = Point(
                    location.longitude,
                    location.latitude,
                    srid=4326
                )
                online_success_count += 1
            else:
                fail_count += 1
                if not TQDM_AVAILABLE:
                    self.stdout.write(self.style.WARNING(
                        f"  ✗ Could not geocode: {station.name} ({station.city}, {station.state})"
                    ))

            batch_buffer.append(station)

            # Batch save every N records for crash safety
            if len(batch_buffer) >= batch_size:
                FuelStation.objects.bulk_update(
                    batch_buffer,
                    ['latitude', 'longitude', 'location'],
                    batch_size=batch_size,
                )
                batch_buffer = []

        # Save remaining records
        if batch_buffer:
            FuelStation.objects.bulk_update(
                batch_buffer,
                ['latitude', 'longitude', 'location'],
                batch_size=batch_size,
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Online Geocoding complete:"
            f"\n    ✓ Success (Full address): {online_success_count - fallback_count}"
            f"\n    ↩ Fallback to city center: {fallback_count}"
            f"\n    ✗ Failed: {fail_count}"
        ))
