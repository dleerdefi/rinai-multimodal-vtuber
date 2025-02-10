# time_tools.py

from time_api_client import TimeApiClient

def get_current_time_in_zone(timeZone: str) -> dict:
    client = TimeApiClient("https://your-timeapi.com")
    try:
        data = client.get_current_time(time_zone=timeZone)
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
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "timeZone": timeZone
        }


def convert_time_between_zones(fromTimeZone: str, dateTime: str, toTimeZone: str) -> dict:
    client = TimeApiClient("https://your-timeapi.com")
    try:
        data = client.convert_time_zone(from_zone=fromTimeZone, date_time=dateTime, to_zone=toTimeZone)
        conversion = data.get("conversionResult", {})
        return {
            "status": "success",
            "fromTimeZone": data.get("fromTimezone"),
            "fromDateTime": data.get("fromDateTime"),
            "toTimeZone": data.get("toTimeZone"),
            "convertedDateTime": conversion.get("dateTime"),
            "dayOfWeek": conversion.get("dayOfWeek"),
            "dstActive": conversion.get("dstActive"),
            "rawData": data
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "fromTimeZone": fromTimeZone,
            "toTimeZone": toTimeZone,
            "dateTime": dateTime
        }
