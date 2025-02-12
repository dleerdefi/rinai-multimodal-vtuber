class ToolPrompts:
    """System prompts for tool analysis and routing"""
    
    TIME_TOOL = """Analyze this time-related request: {command}

Examples:
"what time is it in Tokyo?" -> location="Tokyo", timezone="Asia/Tokyo", action="get_time"
"convert 2pm NYC to London" -> source_timezone="America/New_York", source_time="14:00", timezone="Europe/London", action="convert_time"
"what's the time in Paris?" -> location="Paris", timezone="Europe/Paris", action="get_time"

Respond with valid JSON only:
{{
    "tools_needed": [
        {{
            "tool_name": "time_tools",
            "action": "get_time|convert_time",
            "parameters": {{
                "timezone": "Target timezone/location",
                "source_timezone": "Source timezone (for conversion)",
                "source_time": "Source time (for conversion)"
            }},
            "priority": 1
        }}
    ],
    "reasoning": "Time information or conversion requested"
}}"""

    WEATHER_TOOL = """Analyze this weather-related request and extract parameters: {command}

Examples:
"what's the weather in Paris?" -> location="Paris", units="metric", forecast_type="current"
"how hot is it in Miami fahrenheit" -> location="Miami", units="imperial", forecast_type="current"
"will it rain in London tomorrow" -> location="London", units="metric", forecast_type="daily"
"hourly forecast for NYC" -> location="New York City", units="metric", forecast_type="hourly"

Respond with valid JSON only:
{{
    "tools_needed": [
        {{
            "tool_name": "weather_tools",
            "action": "get_weather",
            "parameters": {{
                "location": "City or location name",
                "units": "metric|imperial",
                "forecast_type": "current|hourly|daily"
            }},
            "priority": 1
        }}
    ],
    "reasoning": "Weather information requested for specific location"
}}"""

    CRYPTO_TOOL = """You are a tool orchestrator that carefully analyzes commands to determine if cryptocurrency tools are required.
DEFAULT BEHAVIOR: Only use crypto_data tool when explicitly asking about cryptocurrency.

Command: "{command}"

Available tool:
crypto_data: ONLY use when explicitly asking about cryptocurrency prices or market data
Example: "What's Bitcoin's price?" or "Show me ETH market data"

Instructions:
- Only use crypto_data for explicit cryptocurrency price/market requests
- Default to get_price action unless specifically requesting detailed market data
- Respond with valid JSON only

Example responses:

For crypto price request:
{{
    "tools_needed": [
        {{
            "tool_name": "crypto_data",
            "action": "get_price",
            "parameters": {{
                "symbol": "BTC",
                "include_details": true
            }},
            "priority": 1
        }}
    ],
    "reasoning": "Explicit request for cryptocurrency price data"
}}

For detailed market data:
{{
    "tools_needed": [
        {{
            "tool_name": "crypto_data",
            "action": "get_market_data",
            "parameters": {{
                "symbol": "ETH",
                "include_details": true
            }},
            "priority": 1
        }}
    ],
    "reasoning": "Request for detailed cryptocurrency market data"
}}"""

    CALENDAR_TOOL = """Analyze this calendar-related request: {command}

Examples:
"what's on my calendar?" -> action="get_schedule", max_events=5
"show my next 3 meetings" -> action="get_schedule", max_events=3
"schedule a meeting with John tomorrow at 2pm" -> action="create_event", summary="Meeting with John", start_time="tomorrow 2pm"
"create a team lunch next Friday at noon" -> action="create_event", summary="Team Lunch", start_time="next Friday 12pm"
"set up a recurring weekly standup at 10am" -> action="create_event", summary="Weekly Standup", start_time="10am", recurrence=["RRULE:FREQ=WEEKLY"]

Respond with valid JSON only:
{{
    "tools_needed": [
        {{
            "tool_name": "calendar_tool",
            "action": "get_schedule|create_event",
            "parameters": {{
                "max_events": "Number of events to fetch (default: 5)",
                "summary": "Event title/summary",
                "start_time": "Event start time",
                "end_time": "Event end time (optional, defaults to 1 hour after start)",
                "location": "Event location (optional)",
                "description": "Event description (optional)",
                "attendees": ["List of email addresses (optional)"],
                "recurrence": ["RRULE strings for recurring events (optional)"],
                "timezone": "Event timezone (default: America/Los_Angeles)"
            }},
            "priority": 1
        }}
    ],
    "reasoning": "Calendar information requested or event creation requested"
}}"""

    CALENDAR_EVENT_APPROVAL = """Analyze the user's response to a calendar event preview: {response}

Examples:
"looks good" -> action="approve", feedback="Event approved"
"yes that works" -> action="approve", feedback="Event approved"
"change the time to 3pm" -> action="modify", changes={"start_time": "3pm"}, feedback="Updating event time to 3pm"
"add John to the invite" -> action="modify", changes={"attendees": ["add: john@example.com"]}, feedback="Adding John to attendees"
"cancel this" -> action="cancel", feedback="Cancelling event creation"

Respond with valid JSON only:
{
    "action": "approve|modify|cancel",
    "changes": {
        "summary": "Updated title (if changed)",
        "start_time": "Updated start time (if changed)",
        "end_time": "Updated end time (if changed)",
        "location": "Updated location (if changed)",
        "description": "Updated description (if changed)",
        "attendees": ["add: email", "remove: email"],
        "recurrence": ["Updated RRULE strings (if changed)"],
        "timezone": "Updated timezone (if changed)"
    },
    "feedback": "Message explaining the action taken"
}"""
