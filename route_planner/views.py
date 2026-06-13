import logging
import time

from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie

from route_planner.serializers import RouteRequestSerializer
from route_planner.services import plan_optimal_route

logger = logging.getLogger(__name__)


class RoutePlannerView(APIView):
    """
    POST /api/route-planner/

    Calculates the most cost-effective fueling strategy for a vehicle
    traveling between two locations in the USA.

    ═══ Request Body (JSON) ═══
    {
        "start_location": "New York, NY",       // or "40.7128,-74.0060"
        "end_location": "Los Angeles, CA"         // or "34.0522,-118.2437"
    }

    ═══ Response ═══
    {
        "total_distance_miles": 2790.5,
        "total_fuel_cost": 872.35,
        "route_geometry": { ... },    // GeoJSON for Leaflet.js
        "fuel_stops": [ ... ],        // Ordered list of optimal stops
        "vehicle_specs": { ... }      // Tank capacity, MPG, etc.
    }
    """

    def post(self, request):
        """Handle route planning request."""
        start_time = time.time()

        # ── Validate input ────────────────────────────────────────────
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'error': 'Invalid input', 'details': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_location = serializer.validated_data['start_location']
        end_location = serializer.validated_data['end_location']

        # ── Run the optimization pipeline ─────────────────────────────
        try:
            result = plan_optimal_route(start_location, end_location)
        except ValueError as e:
            # Geocoding failures, infeasible routes, etc.
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RuntimeError as e:
            # OSRM API failures, algorithm bugs
            return Response(
                {'error': str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            logger.exception(f"Unexpected error planning route: {e}")
            return Response(
                {'error': 'An unexpected error occurred. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── Serialize and return ──────────────────────────────────────
        elapsed_ms = (time.time() - start_time) * 1000
        result['processing_time_ms'] = round(elapsed_ms, 1)

        logger.info(
            f"Route planned: {start_location} → {end_location} | "
            f"{result['total_distance_miles']} mi | "
            f"${result['total_fuel_cost']} | "
            f"{result['num_fuel_stops']} stops | "
            f"{elapsed_ms:.0f}ms"
        )

        return Response(result, status=status.HTTP_200_OK)

    def get(self, request):
        """
        GET /api/route-planner/

        Returns API documentation and usage instructions.
        """
        return Response({
            'message': 'Fuel-Efficient Route Planner API',
            'version': '1.0',
            'usage': {
                'method': 'POST',
                'url': '/api/route-planner/',
                'content_type': 'application/json',
                'body': {
                    'start_location': 'string — Place name or "lat,lng" coordinates',
                    'end_location': 'string — Place name or "lat,lng" coordinates',
                },
                'examples': [
                    {
                        'start_location': 'New York, NY',
                        'end_location': 'Los Angeles, CA',
                    },
                    {
                        'start_location': '40.7128,-74.0060',
                        'end_location': '34.0522,-118.2437',
                    },
                ],
            },
            'vehicle_specs': {
                'tank_capacity': f"{getattr(settings, 'VEHICLE_TANK_CAPACITY_GALLONS', 50)} gallons",
                'fuel_efficiency': f"{getattr(settings, 'VEHICLE_MPG', 10)} MPG",
                'max_range': f"{getattr(settings, 'VEHICLE_MAX_RANGE_MILES', 500)} miles per full tank",
                'starts_with': 'Full tank',
            },
            'algorithm': 'To Fill or Not to Fill (Khuller et al., 2007) — Provably optimal',
        })


@method_decorator(ensure_csrf_cookie, name='dispatch')
class MapView(TemplateView):
    """
    GET /

    Serves the Leaflet.js map frontend for interactive route visualization.
    """
    template_name = 'route_planner/index.html'
