from django.contrib.gis import admin
from route_planner.models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.GISModelAdmin):
    """Admin interface for managing fuel stations."""

    list_display = ('name', 'city', 'state', 'retail_price', 'is_geocoded', 'opis_id')
    list_filter = ('state',)
    search_fields = ('name', 'city', 'address', 'opis_id')
    ordering = ('retail_price',)
    readonly_fields = ('latitude', 'longitude')

    list_per_page = 50
