import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Update the path resolution
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)  # Points to project root
sys.path.append(project_root)

# Load environment variables before imports
load_dotenv(dotenv_path=Path(project_root) / '.env')

# Verify environment variables are loaded
required_vars = [
    'GOOGLE_CREDENTIALS_PATH',
    'GOOGLE_CLIENT_ID',
    'GOOGLE_CLIENT_SECRET'
]

# Check if all required variables are present
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Print loaded values for debugging (remove in production)
print(f"Credentials Path: {os.getenv('GOOGLE_CREDENTIALS_PATH')}")
print(f"Client ID: {os.getenv('GOOGLE_CLIENT_ID')}")
print(f"Client Secret: {os.getenv('GOOGLE_CLIENT_SECRET')}")

# Import after environment variables are loaded
from src.clients.google_calendar_client import GoogleCalendarClient
from src.tools.calendar_tool import CalendarTool

import pytest
import logging

# Set up logging
logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_calendar_client_initialization():
    """Test calendar client initialization"""
    client = GoogleCalendarClient()
    success = await client.initialize()
    assert success, "Calendar client initialization failed"
    assert client.service is not None, "Calendar service not initialized"

@pytest.mark.asyncio
async def test_calendar_events_fetch():
    """Test fetching calendar events"""
    client = GoogleCalendarClient()
    await client.initialize()
    
    events = await client.get_upcoming_events(max_results=5)
    assert isinstance(events, list), "Events should be a list"
    
    if events:  # If there are events
        event = events[0]
        assert 'summary' in event, "Event should have a summary"
        assert 'start' in event, "Event should have a start time"

@pytest.mark.asyncio
async def test_calendar_tool_integration():
    """Test calendar tool full integration"""
    client = GoogleCalendarClient()
    tool = CalendarTool(calendar_client=client)
    
    # Initialize tool
    success = await tool.initialize()
    assert success, "Tool initialization failed"
    
    # Test get_schedule
    result = await tool.get_schedule(max_events=3)
    assert result["status"] in ["success", "error"]
    assert "response" in result
    assert "requires_tts" in result
    
    if result["status"] == "success":
        assert isinstance(result.get("data", []), list)

@pytest.mark.asyncio
async def test_calendar_response_formatting():
    """Test calendar response formatting"""
    tool = CalendarTool()
    
    # Test with empty events
    empty_response = tool._format_calendar_response([])
    assert "No upcoming events found" in empty_response
    
    # Test with mock events
    mock_events = [
        {
            'summary': 'Team Meeting',
            'start': {'dateTime': (datetime.now() + timedelta(hours=1)).isoformat() + 'Z'}
        },
        {
            'summary': 'Lunch with Client',
            'start': {'dateTime': (datetime.now() + timedelta(hours=3)).isoformat() + 'Z'}
        }
    ]
    
    formatted_response = tool._format_calendar_response(mock_events)
    assert "📅" in formatted_response
    assert "Team Meeting" in formatted_response
    assert "Lunch with Client" in formatted_response

if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    
    # Run tests
    pytest.main([__file__, "-v"]) 