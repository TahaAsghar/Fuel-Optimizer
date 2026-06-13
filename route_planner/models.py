from django.contrib.gis.db import models


class FuelStation(models.Model):
    """
    A truck fuel stop with location, address details, and retail diesel price.

    Key design decisions:
    - opis_id is NOT unique because the raw CSV has duplicate OPIS IDs
      with different rack prices. We import only the cheapest-priced row
      per OPIS ID, but keep the field non-unique for safety.
    - location is a PostGIS PointField (SRID 4326 = WGS84 / GPS coords),
      enabling fast ST_DWithin spatial queries.
    - geography=True means distances are computed on the earth's surface
      (great-circle), not on a flat Cartesian plane.
    """

    opis_id = models.IntegerField(
        db_index=True,
    )
    name = models.CharField(
        max_length=255,
    )
    address = models.CharField(
        max_length=255,
    )
    city = models.CharField(
        max_length=100,
    )
    state = models.CharField(
        max_length=10,
    )
    rack_id = models.IntegerField(
        help_text="Rack ID from the pricing data"
    )
    retail_price = models.FloatField(
        db_index=True,
    )

    # ── PostGIS Spatial Field ──────────────────────────────────────────
    location = models.PointField(
        srid=4326,     
        geography=True,
        null=True,
        blank=True,
    )

    # ── Convenience fields for direct coordinate access ────────────────
    latitude = models.FloatField(
        null=True, blank=True, db_index=True,
    )
    longitude = models.FloatField(
        null=True, blank=True, db_index=True,
    )

    class Meta:
        ordering = ['retail_price']
        indexes = [
            # Composite index for bounding-box pre-filtering
            models.Index(fields=['latitude', 'longitude'], name='idx_lat_lng'),
        ]
        verbose_name = "Fuel Station"
        verbose_name_plural = "Fuel Stations"

    def __str__(self):
        return f"{self.name} — ${self.retail_price:.3f}/gal ({self.city}, {self.state})"

    @property
    def is_geocoded(self):
        """Check if this station has been successfully geocoded."""
        return self.location is not None
