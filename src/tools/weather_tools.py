import geopandas as gpd
from openmeteo_requests import Client
from openmeteo_sdk.Variable import Variable

def get_weather_data(location: str) -> dict:
    """
    Given a user-provided location, geocode it to latitude/longitude 
    and fetch the current weather using Open-Meteo.

    Returns a dictionary containing basic weather info.
    """
    # 1. Geocode the location
    #    By default, geocode() uses the Photon geocoding service.
    #    You can also specify: provider="nominatim", user_agent="my-app" 
    #    if you prefer OpenStreetMap's Nominatim. 
    gdf = gpd.tools.geocode(location)

    if gdf.empty:
        return {
            "status": "error",
            "message": f"Could not geocode location: {location}"
        }

    # Extract lat/lon (GeoDataFrame is in EPSG:4326 if provider = "nominatim"/"photon")
    # geometry.x → longitude, geometry.y → latitude
    latitude = gdf.geometry.y.iloc[0]
    longitude = gdf.geometry.x.iloc[0]

    # 2. Query Open-Meteo for forecast/current weather
    client = Client()
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["temperature_2m", "precipitation", "wind_speed_10m"],
        "current": ["temperature_2m", "relative_humidity_2m"]
    }

    try:
        responses = client.weather_api("https://api.open-meteo.com/v1/forecast", params=params)
        response = responses[0]  # Only one location in this example
        
        # 3. Extract current data
        current = response.Current()
        current_vars = [
            current.Variables(i) for i in range(current.VariablesLength())
        ]
        current_temp = next(
            v for v in current_vars
            if v.Variable() == Variable.temperature and v.Altitude() == 2
        )
        current_humidity = next(
            v for v in current_vars
            if v.Variable() == Variable.relative_humidity and v.Altitude() == 2
        )

        data = {
            "status": "success",
            "location_searched": location,
            "latitude": latitude,
            "longitude": longitude,
            "current_time": current.Time(),
            "current_temperature_c": current_temp.Value(),
            "current_relative_humidity_percent": current_humidity.Value()
        }
        return data

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "location_searched": location
        }
