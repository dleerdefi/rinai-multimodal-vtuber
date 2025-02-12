import aiohttp  # Use aiohttp for async HTTP requests
from typing import Optional, Dict

class TimeApiClient:
    """

    A simple client to interact with timeapi.io, providing
    methods to get the current time in a specific IANA time zone.
    """

    def __init__(self, base_url: str = "https://timeapi.io"):
        """
        :param base_url: The root URL of the time API (defaults to timeapi.io).
        """
        self.base_url = base_url.rstrip("/")

    async def get_current_time(self, time_zone: str) -> Optional[Dict]:
        """
        Calls GET /api/time/current/zone?timeZone=<time_zone> to get
        the current date/time info from timeapi.io.

        :param time_zone: Full IANA time zone, e.g. 'Europe/Amsterdam'
        :return: A dictionary with time data or error info.
        """
        endpoint = f"{self.base_url}/api/time/current/zone"
        params = {"timeZone": time_zone}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, params=params, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Example data structure (from your docs):
                        # {
                        #   "year": 2025, "month": 2, "day": 10,
                        #   "hour": 11, "minute": 1, "seconds": 10,
                        #   "milliSeconds": 816,
                        #   "dateTime": "2025-02-10T11:01:10.8161637",
                        #   "date": "02/10/2025", "time": "11:01",
                        #   "timeZone": "Europe/Amsterdam", "dayOfWeek": "Monday",
                        #   "dstActive": false
                        # }

                        return {
                            "status": "success",
                            "timeZone": data.get("timeZone"),
                            "dateTime": data.get("dateTime"),
                            "date": data.get("date"),
                            "time": data.get("time"),
                            "dayOfWeek": data.get("dayOfWeek"),
                            "dstActive": data.get("dstActive"),
                            "rawData": data
                        }
            return None
        except Exception as e:
            return None

    async def convert_time_zone(self, from_zone: str, date_time: str, to_zone: str) -> Optional[Dict]:
        """Convert time between timezones"""
        endpoint = f"{self.base_url}/api/time/convert"
        params = {
            "fromTimeZone": from_zone,
            "dateTime": date_time,
            "toTimeZone": to_zone
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, params=params, timeout=5) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            return None
