# future use
# import requests

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

    def get_current_time(self, time_zone: str) -> dict:
        """
        Calls GET /api/time/current/zone?timeZone=<time_zone> to get
        the current date/time info from timeapi.io.

        :param time_zone: Full IANA time zone, e.g. 'Europe/Amsterdam'
        :return: A dictionary with time data or error info.
        """
        endpoint = f"{self.base_url}/api/time/current/zone"
        params = {"timeZone": time_zone}
        
        try:
            response = requests.get(endpoint, params=params)
            response.raise_for_status()
            data = response.json()
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
        except requests.exceptions.RequestException as e:
            return {
                "status": "error",
                "message": str(e),
                "timeZone": time_zone
            }
