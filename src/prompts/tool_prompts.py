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
