from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start_location = serializers.CharField(
        max_length=500
    )
    end_location = serializers.CharField(
        max_length=500
    )

    def validate_start_location(self, value):
        """Strip whitespace and validate non-empty."""
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Start location cannot be empty.")
        return value

    def validate_end_location(self, value):
        """Strip whitespace and validate non-empty."""
        value = value.strip()
        if not value:
            raise serializers.ValidationError("End location cannot be empty.")
        return value

    def validate(self, data):
        if data['start_location'].lower() == data['end_location'].lower():
            raise serializers.ValidationError(
                "Start and end locations must be different."
            )
        return data


class FuelStopSerializer(serializers.Serializer):
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    distance_along_route_miles = serializers.FloatField()
    gallons_to_add = serializers.FloatField()
    cost = serializers.FloatField()
    fuel_level_before = serializers.FloatField()
    fuel_level_after = serializers.FloatField()


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class VehicleSpecsSerializer(serializers.Serializer):
    tank_capacity_gallons = serializers.FloatField()
    mpg = serializers.FloatField()
    max_range_miles = serializers.FloatField()
    starts_with_full_tank = serializers.BooleanField()


class RouteResponseSerializer(serializers.Serializer):

    start_location = LocationSerializer()
    end_location = LocationSerializer()
    total_distance_miles = serializers.FloatField()
    total_duration_hours = serializers.FloatField()
    total_fuel_cost = serializers.FloatField()
    fuel_remaining_gallons = serializers.FloatField()
    num_fuel_stops = serializers.IntegerField()
    route_geometry = serializers.DictField()
    fuel_stops = FuelStopSerializer(many=True)
    vehicle_specs = VehicleSpecsSerializer()
