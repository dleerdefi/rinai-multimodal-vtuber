import logging
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, UTC
import geopandas as gpd
# Updated imports for Open-Meteo
import openmeteo_requests
import requests_cache
from retry_requests import retry
from openmeteo_sdk.Variable import Variable
from src.tools.base import BaseTool, AgentResult, WeatherToolParameters
from src.services.llm_service import LLMService
import json
from src.prompts.tool_prompts import ToolPrompts
import requests

logger = logging.getLogger(__name__)

class WeatherTool(BaseTool):
    """Tool for handling weather-related operations"""
    
    def __init__(self):
        super().__init__()
        self.name = "weather_tools"
        self.description = "Tool for weather information"
        self.version = "1.0.0"
        
        # Setup the Open-Meteo client with retry logic
        retry_session = retry(retries=3, backoff_factor=0.5)
        self.client = openmeteo_requests.Client(session=retry_session)
        
        # Initialize LLM for natural language processing
        self.llm_service = LLMService()
        
    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Main execution method"""
        return await self.execute(input_data)

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by weather tool"""
        return isinstance(input_data, (dict, WeatherToolParameters))

    async def execute(self, command: Dict) -> Dict:
        """Execute weather command"""
        try:
            if not isinstance(command, dict):
                return {
                    "status": "error",
                    "error": "Invalid input format",
                    "timestamp": datetime.utcnow().isoformat()
                }

            result = await self.get_weather_data(
                location=command.get("location"),
                units=command.get("units", "metric"),
                forecast_type=command.get("forecast_type", "current")
            )

            # Format the response to match orchestrator's expectations
            return {
                "status": "success",
                "response": self._format_weather_response(result),
                "requires_tts": True,  # Since we have emojis
                "data": result,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"Error executing weather command: {e}")
            return {
                "status": "error",
                "response": f"Error: {str(e)}",
                "requires_tts": True,
                "timestamp": datetime.utcnow().isoformat()
            }

    def _clean_location(self, location: str) -> str:
        """Clean location string from query"""
        # Remove common weather-related words
        clean_words = [
            "weather", "in", "at", "for", "of", "the", 
            "forecast", "temperature", "current", "conditions"
        ]
        
        # Convert to lowercase and split into words
        words = location.lower().split()
        
        # Remove weather-related words
        location_words = [word for word in words if word not in clean_words]
        
        # Join remaining words back together
        return " ".join(location_words).title()

    async def _analyze_weather_query(self, query: str) -> Dict:
        """Use LLM to analyze weather query for location and time intent"""
        try:
            messages = [
                {"role": "system", "content": ToolPrompts.WEATHER_TOOL},
                {"role": "user", "content": query}
            ]
            
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type="groq-llama",
                override_config={"temperature": 0.1}
            )
            
            analysis = json.loads(response)
            return {
                "location": analysis.get("location", ""),
                "forecast_type": analysis.get("forecast_type", "current"),
                "specific_metrics": analysis.get("specific_metrics", [])
            }
        except Exception as e:
            logger.error(f"Error analyzing weather query: {e}")
            return {"location": query, "forecast_type": "current", "specific_metrics": []}

    async def get_weather_data(
        self, 
        location: str, 
        units: str = "metric",
        forecast_type: str = "current"
    ) -> Dict:
        """Get weather data for a location"""
        try:
            # Try to get from cache first
            cache_key = f"weather_{location}_{units}_{forecast_type}"
            result = await self.get_cached_or_fetch(
                cache_key,
                lambda: self._fetch_weather_data(location, units, forecast_type)
            )
            
            # Format the response here
            if result.get("status") == "success":
                formatted_response = self._format_weather_response(result)
                return {
                    "status": "success",
                    "response": formatted_response,
                    "requires_tts": True,  # For emoji handling
                    "data": result
                }
            return result
            
        except Exception as e:
            logger.error(f"Error getting weather data: {e}")
            return {
                "status": "error",
                "message": str(e),
                "location": location
            }

    async def _fetch_weather_data(
        self, 
        location: str, 
        units: str,
        forecast_type: str
    ) -> Dict:
        """Fetch fresh weather data from the API"""
        try:
            # Geocode the location
            coords = await self._geocode_location(location)
            if not coords:
                return {
                    "status": "error",
                    "message": f"Could not geocode location: {location}"
                }
                
            lat, lon = coords
            
            # Prepare API parameters based on forecast type
            params = {
                "latitude": lat,
                "longitude": lon,
                "timezone": "auto",
                "current": ["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"],
            }
            
            if forecast_type in ["hourly", "daily"]:
                params[forecast_type] = [
                    "temperature_2m",
                    "precipitation_probability",
                    "wind_speed_10m"
                ]
                if forecast_type == "daily":
                    params["forecast_days"] = 7
            
            # Get weather data with retry logic
            try:
                responses = self.client.weather_api(
                    "https://api.open-meteo.com/v1/forecast",
                    params=params
                )
                response = responses[0]
            except Exception as api_error:
                logger.error(f"API request failed: {api_error}")
                raise
            
            # Extract current conditions
            current = response.Current()
            current_vars = [
                current.Variables(i) for i in range(current.VariablesLength())
            ]
            
            # Format current conditions
            current_data = {
                "temperature": self._format_temperature(
                    self._get_variable_value(current_vars, Variable.temperature, 2),
                    units
                ),
                "humidity": f"{self._get_variable_value(current_vars, Variable.relative_humidity, 2)}%",
                "precipitation": f"{self._get_variable_value(current_vars, Variable.precipitation)}mm",
                "wind_speed": f"{self._get_variable_value(current_vars, Variable.wind_speed, 10)} km/h",
                "timestamp": self._format_timestamp(current.Time())
            }
            
            # Add forecast data if requested
            result = {
                "status": "success",
                "location": location,
                "coordinates": {"lat": lat, "lon": lon},
                "current": current_data
            }
            
            if forecast_type != "current":
                result["forecast"] = self._extract_forecast_data(
                    response, 
                    forecast_type,
                    units
                )
            
            return result
            
        except Exception as e:
            logger.error(f"Error fetching weather: {e}")
            raise

    def _get_variable_value(
        self, 
        variables: list, 
        var_type: Variable, 
        altitude: int = None
    ) -> Optional[float]:
        """Helper to extract variable value from OpenMeteo response"""
        try:
            if altitude:
                var = next(
                    (v for v in variables 
                     if v.Variable() == var_type and v.Altitude() == altitude),
                    None
                )
            else:
                var = next(
                    (v for v in variables if v.Variable() == var_type),
                    None
                )
            return var.Value() if var else None
        except Exception:
            return None

    def _format_timestamp(self, timestamp: str) -> str:
        """Convert timestamp to human-readable format"""
        try:
            dt = datetime.fromisoformat(timestamp)
            return dt.strftime("%A, %I:%M %p")
        except Exception:
            return timestamp

    def _format_temperature(self, temp: float, units: str) -> str:
        """Format temperature based on units"""
        if temp is None:
            return "N/A"
        if units == "imperial":
            temp = (temp * 9/5) + 32
            return f"{temp:.1f}°F"
        return f"{temp:.1f}°C"

    async def _geocode_location(self, location: str) -> Optional[Tuple[float, float]]:
        """Enhanced geocoding with fallback and validation"""
        try:
            # Try primary geocoding
            geo_url = f"https://nominatim.openstreetmap.org/search"
            params = {
                "q": location,
                "format": "json",
                "limit": 1
            }
            headers = {
                "User-Agent": "RinAI/1.0"
            }
            
            response = requests.get(geo_url, params=params, headers=headers)
            data = response.json()

            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
                
            # If no results, try with additional context
            logger.info(f"No results for {location}, trying with additional context")
            return None
            
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
            return None

    def _extract_forecast_data(
        self, 
        response: Any, 
        forecast_type: str,
        units: str
    ) -> Dict:
        """Extract and format forecast data from response"""
        try:
            if forecast_type == "hourly":
                hourly = response.Hourly()
                return {
                    "intervals": [
                        {
                            "time": self._format_timestamp(hourly.Time(i)),
                            "temperature": self._format_temperature(
                                hourly.Variables(0).ValuesArray(i),
                                units
                            ),
                            "precipitation_prob": f"{hourly.Variables(1).ValuesArray(i)}%",
                            "wind_speed": f"{hourly.Variables(2).ValuesArray(i)} km/h"
                        }
                        for i in range(24)  # Next 24 hours
                    ]
                }
            elif forecast_type == "daily":
                daily = response.Daily()
                return {
                    "days": [
                        {
                            "date": self._format_timestamp(daily.Time(i)),
                            "temperature": self._format_temperature(
                                daily.Variables(0).ValuesArray(i),
                                units
                            ),
                            "precipitation_prob": f"{daily.Variables(1).ValuesArray(i)}%",
                            "wind_speed": f"{daily.Variables(2).ValuesArray(i)} km/h"
                        }
                        for i in range(7)  # 7-day forecast
                    ]
                }
            return {}
            
        except Exception as e:
            logger.error(f"Error extracting forecast data: {e}")
            return {}

    def _format_weather_response(self, result: Dict) -> str:
        """Format weather data into human readable response"""
        if result.get("status") == "error":
            return f"Sorry, {result.get('message', 'an error occurred')}"
            
        response_parts = []
        response_parts.append(f"Weather in {result['location']}:")
        
        if "current" in result:
            current = result["current"]
            response_parts.append(f"🌡️ Temperature: {current['temperature']}")
            response_parts.append(f"💧 Humidity: {current['humidity']}")
            response_parts.append(f"🌧️ Precipitation: {current['precipitation']}")
            response_parts.append(f"💨 Wind Speed: {current['wind_speed']}")
            
        if "forecast" in result:
            if "intervals" in result["forecast"]:
                response_parts.append("\nHourly Forecast:")
                for interval in result["forecast"]["intervals"][:8]:  # Show next 8 hours
                    response_parts.append(f"{interval['time']}: {interval['temperature']}")
            elif "days" in result["forecast"]:
                response_parts.append("\nDaily Forecast:")
                for day in result["forecast"]["days"][:3]:  # Show next 3 days
                    response_parts.append(f"{day['date']}: {day['temperature']}")
                    
        return "\n".join(response_parts)