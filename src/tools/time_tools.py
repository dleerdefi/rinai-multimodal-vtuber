# time_tools.py

from datetime import datetime
import logging
from typing import Dict, Optional, Any
import requests
from dateutil import parser as date_parser
from geopy.geocoders import Nominatim
from src.tools.base import BaseTool, AgentResult, TimeToolParameters
from src.clients.time_api_client import TimeApiClient
from src.services.llm_service import LLMService
import json
from src.prompts.tool_prompts import ToolPrompts

logger = logging.getLogger(__name__)

class TimeTool(BaseTool):
    """Tool for handling time-related operations"""
    
    def __init__(self):
        super().__init__()
        self.name = "time_tools"
        self.description = "Tool for time and timezone operations"
        self.version = "1.0.0"
        self.client = TimeApiClient("https://timeapi.io")
        self.backup_api = "https://worldtimeapi.org/api/timezone"
        self.geolocator = Nominatim(user_agent="time_bot")
        self.llm_service = LLMService()
        
    async def execute(self, command: Dict) -> Dict:
        """Execute time command with standardized parameters"""
        try:
            action = command.get("action")
            if action == "get_time":
                return await self.get_current_time_in_zone(
                    command.get("timezone")
                )
            elif action == "convert_time":
                return await self.convert_time_between_zones(
                    from_zone=command.get("source_timezone"),
                    date_time=command.get("source_time"),
                    to_zone=command.get("timezone")
                )
            else:
                return {
                    "status": "error",
                    "error": f"Unknown action: {action}",
                    "timestamp": datetime.utcnow().isoformat()
                }
        except Exception as e:
            logger.error(f"Error executing time command: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Required implementation of BaseTool's abstract run method"""
        try:
            if isinstance(input_data, dict):
                result = await self.execute(input_data)
                # Wrap response in AgentResult
                return AgentResult(
                    response=self._format_time_response(result),
                    data=result
                ).dict()
            return AgentResult(
                response="Invalid input format",
                data={
                    "status": "error",
                    "error": "Invalid input format",
                    "timestamp": datetime.utcnow().isoformat()
                }
            ).dict()
        except Exception as e:
            return AgentResult(
                response=f"Error: {str(e)}",
                data={
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat()
                }
            ).dict()

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by time tool"""
        return isinstance(input_data, (dict, TimeToolParameters))

    async def get_current_time_in_zone(self, location_or_timezone: str) -> Dict:
        """Get current time for a location or timezone"""
        try:
            # Try to get timezone from location if not a timezone string
            timezone = await self._resolve_timezone(location_or_timezone)
            if not timezone:
                return {
                    "status": "error",
                    "message": f"Could not determine timezone for: {location_or_timezone}"
                }

            # Try cache first
            cache_key = f"time_{timezone}"
            data = await self.get_cached_or_fetch(
                cache_key,
                lambda: self._fetch_time_data(timezone)
            )
            
            if not data:
                return {
                    "status": "error",
                    "message": "Failed to fetch time data"
                }
            
            return {
                "status": "success",
                "location": location_or_timezone,
                "timezone": timezone,
                "current_time": self._format_time(data.get("dateTime")),
                "day_of_week": data.get("dayOfWeek"),
                "dst_active": data.get("dstActive")
            }

        except Exception as e:
            logger.error(f"Error getting current time: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def convert_time_between_zones(
        self,
        from_zone: str,
        date_time: str,
        to_zone: str
    ) -> Dict:
        """Convert time between timezones"""
        try:
            # Resolve both timezones if they're locations
            from_timezone = await self._resolve_timezone(from_zone)
            to_timezone = await self._resolve_timezone(to_zone)
            
            if not from_timezone or not to_timezone:
                return {
                    "status": "error",
                    "message": "Could not resolve one or both timezones"
                }

            # Parse the input time
            parsed_time = self._parse_user_time(date_time)
            if not parsed_time:
                return {
                    "status": "error",
                    "message": f"Could not parse time format: {date_time}"
                }

            data = await self.client.convert_time_zone(
                from_zone=from_timezone,
                date_time=parsed_time.isoformat(),
                to_zone=to_timezone
            )
            
            return {
                "status": "success",
                "from_location": from_zone,
                "to_location": to_zone,
                "from_time": self._format_time(parsed_time.isoformat()),
                "converted_time": self._format_time(data.get("convertedDateTime")),
                "from_timezone": from_timezone,
                "to_timezone": to_timezone
            }

        except Exception as e:
            logger.error(f"Error converting time: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def _resolve_timezone(self, location_or_timezone: str) -> Optional[str]:
        """Resolve timezone from location using multiple fallbacks"""
        try:
            logger.info(f"Resolving timezone for: {location_or_timezone}")
            
            # Common timezone mappings with variants
            timezone_mappings = {
                # Asia
                "tokyo": "Asia/Tokyo",
                "toyko": "Asia/Tokyo",  # Common misspelling
                "beijing": "Asia/Shanghai",
                "shanghai": "Asia/Shanghai",
                "hong kong": "Asia/Hong_Kong",
                "hongkong": "Asia/Hong_Kong",
                "singapore": "Asia/Singapore",
                "dubai": "Asia/Dubai",
                
                # Europe
                "london": "Europe/London",
                "paris": "Europe/Paris",
                "berlin": "Europe/Berlin",
                "moscow": "Europe/Moscow",
                "amsterdam": "Europe/Amsterdam",
                "rome": "Europe/Rome",
                
                # Americas
                "new york": "America/New_York",
                "nyc": "America/New_York",
                "los angeles": "America/Los_Angeles",
                "la": "America/Los_Angeles",
                "chicago": "America/Chicago",
                "toronto": "America/Toronto",
                
                # Australia/Pacific
                "sydney": "Australia/Sydney",
                "melbourne": "Australia/Melbourne",
                "auckland": "Pacific/Auckland"
            }
            
            # First check if it's already a valid timezone
            if "/" in location_or_timezone:
                logger.info(f"Valid timezone format detected: {location_or_timezone}")
                return location_or_timezone
                
            # Clean and check location
            location_lower = location_or_timezone.lower().strip()
            logger.info(f"Cleaned location: {location_lower}")
            
            # Direct mapping check
            if location_lower in timezone_mappings:
                timezone = timezone_mappings[location_lower]
                logger.info(f"Found direct mapping: {timezone}")
                return timezone
                
            # Try partial matches
            for key, value in timezone_mappings.items():
                if key in location_lower or location_lower in key:
                    logger.info(f"Found partial match: {value} for {location_lower}")
                    return value
            
            # If no mapping found, try backup API
            logger.info("No mapping found, trying backup API...")
            try:
                backup_response = requests.get(
                    f"{self.backup_api}/{location_or_timezone}",
                    timeout=5
                )
                
                if backup_response.status_code == 200:
                    timezone = backup_response.json().get("timezone")
                    logger.info(f"Found timezone from API: {timezone}")
                    return timezone
                    
            except Exception as backup_error:
                logger.warning(f"Backup API failed: {backup_error}")
            
            logger.warning(f"Could not resolve timezone for: {location_or_timezone}")
            return None

        except Exception as e:
            logger.error(f"Error resolving timezone: {e}")
            return None

    def _parse_user_time(self, time_str: str) -> Optional[datetime]:
        """Parse various time formats using dateutil"""
        try:
            return date_parser.parse(time_str)
        except Exception as e:
            logger.error(f"Error parsing time string: {e}")
            return None

    def _format_time(self, timestamp: str) -> str:
        """Format timestamp into human-readable format"""
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return dt.strftime("%A, %I:%M %p %Z")
        except Exception:
            return timestamp

    async def _fetch_time_data(self, timezone: str) -> Optional[Dict]:
        """Fetch time data with fallback to backup API"""
        try:
            # Try primary API first
            data = await self.client.get_current_time(timezone)
            if data:
                return data
                
            # If primary fails, try backup API
            backup_response = requests.get(f"{self.backup_api}/{timezone}")
            if backup_response.status_code == 200:
                backup_data = backup_response.json()
                return {
                    "dateTime": backup_data.get("datetime"),
                    "dayOfWeek": datetime.fromisoformat(
                        backup_data.get("datetime").replace('Z', '+00:00')
                    ).strftime("%A"),
                    "dstActive": backup_data.get("dst")
                }
                
            return None
            
        except Exception as e:
            logger.error(f"Error fetching time data: {e}")
            return None

    def _format_time_response(self, result: Dict) -> str:
        """Format time data into human readable response"""
        if result.get("status") == "error":
            return f"Sorry, {result.get('message', 'an error occurred')}"
            
        if "current_time" in result:
            return f"The current time in {result['location']} is {result['current_time']}"
        elif "converted_time" in result:
            return f"When it's {result['from_time']} in {result['from_location']}, it's {result['converted_time']} in {result['to_location']}"
        
        return "I couldn't process that time request"
