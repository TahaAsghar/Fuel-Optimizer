from django.urls import path
from route_planner.views import RoutePlannerView, MapView

urlpatterns = [
    # API endpoint
    path('api/route-planner/', RoutePlannerView.as_view(), name='route-planner-api'),

    # Map frontend
    path('', MapView.as_view(), name='map-home'),
]
