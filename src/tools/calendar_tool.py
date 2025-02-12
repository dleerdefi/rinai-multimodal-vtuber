# Standard library imports
import logging
from datetime import datetime
from typing import Dict, Optional, List, Any

# Base imports
from src.tools.base import BaseTool, CalendarToolParameters

# Client imports
from src.clients.google_calendar_client import GoogleCalendarClient

# Initialize logger
logger = logging.getLogger(__name__)

class CalendarTool(BaseTool):
    """Tool for handling calendar operations"""
    
    def __init__(self, calendar_client: Optional[GoogleCalendarClient] = None):
        super().__init__()
        self.name = "calendar_tool"
        self.description = "Tool for accessing and managing calendar events"
        self.version = "1.0.0"
        self.calendar_client = calendar_client
        self.cache_ttl = 60  # 1 minute cache for calendar events
        
    async def initialize(self) -> bool:
        """Initialize the calendar tool"""
        if self.calendar_client:
            return await self.calendar_client.initialize()
        return False

    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Main execution method"""
        return await self.execute(input_data)

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by calendar tool"""
        return isinstance(input_data, (dict, CalendarToolParameters))

    async def execute(self, command: Dict) -> Dict:
        """Execute calendar command"""
        try:
            if not isinstance(command, dict):
                return {
                    "status": "error",
                    "response": "Invalid input format",
                    "requires_tts": True,
                    "timestamp": datetime.utcnow().isoformat()
                }

            action = command.get("action", "get_schedule")
            
            if action == "get_schedule":
                return await self.get_schedule(
                    max_events=command.get("max_events", 5),
                    time_min=command.get("time_min"),
                    time_max=command.get("time_max")
                )
            elif action == "create_event":
                return await self.create_event(
                    summary=command.get("summary"),
                    start_time=command.get("start_time"),
                    end_time=command.get("end_time"),
                    location=command.get("location"),
                    description=command.get("description"),
                    attendees=command.get("attendees", []),
                    recurrence=command.get("recurrence", []),
                    timezone=command.get("timezone", "America/Los_Angeles")
                )
            else:
                return {
                    "status": "error",
                    "response": f"Unknown action: {action}",
                    "requires_tts": True,
                    "timestamp": datetime.utcnow().isoformat()
                }

        except Exception as e:
            logger.error(f"Error executing calendar command: {e}")
            return {
                "status": "error",
                "response": "I encountered an error while accessing your calendar.",
                "requires_tts": True,
                "timestamp": datetime.utcnow().isoformat()
            }

    async def create_event(self, 
                          summary: str,
                          start_time: str,
                          end_time: str,
                          location: Optional[str] = None,
                          description: Optional[str] = None,
                          attendees: Optional[List[Dict]] = None,
                          recurrence: Optional[List[str]] = None,
                          timezone: str = "America/Los_Angeles") -> Dict:
        """Create a calendar event"""
        try:
            if not self.calendar_client:
                return {
                    "status": "error",
                    "response": "Calendar service not configured",
                    "requires_tts": True,
                    "timestamp": datetime.utcnow().isoformat()
                }

            event = {
                'summary': summary,
                'start': {
                    'dateTime': start_time,
                    'timeZone': timezone,
                },
                'end': {
                    'dateTime': end_time,
                    'timeZone': timezone,
                }
            }

            if location:
                event['location'] = location
            if description:
                event['description'] = description
            if attendees:
                event['attendees'] = attendees
            if recurrence:
                event['recurrence'] = recurrence

            created_event = await self.calendar_client.create_event(event)
            
            return {
                "status": "success",
                "response": f"Event '{summary}' has been created successfully!",
                "requires_tts": True,
                "data": created_event,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"Error creating event: {e}")
            return {
                "status": "error",
                "response": "I couldn't create the calendar event.",
                "requires_tts": True,
                "timestamp": datetime.utcnow().isoformat()
            }

    async def get_schedule(self, max_events: int = 5, time_min: str = None, time_max: str = None) -> Dict:
        """Get upcoming calendar events
        
        Args:
            max_events: Maximum number of events to return
            time_min: Start time in ISO format (optional)
            time_max: End time in ISO format (optional)
        """
        try:
            if not self.calendar_client:
                return {
                    "status": "error",
                    "response": "Calendar service not configured",
                    "requires_tts": True,
                    "timestamp": datetime.utcnow().isoformat()
                }

            # Convert max_events to maxResults for the API call
            events = await self.calendar_client.get_upcoming_events(
                maxResults=max_events,  # Changed from max_events to maxResults
                time_min=time_min,
                time_max=time_max
            )
            
            return {
                "status": "success",
                "response": self._format_calendar_response(events),
                "requires_tts": True,
                "data": events,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting schedule: {e}")
            return {
                "status": "error",
                "response": "I couldn't access your calendar right now.",
                "requires_tts": True,
                "timestamp": datetime.utcnow().isoformat()
            }

    def _format_calendar_response(self, events: List[Dict]) -> str:
        """Format calendar events into readable response"""
        if not events:
            return "No upcoming events found."
            
        response = ["📅 Here are your upcoming events:"]
        
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            
            # Add emoji based on event type/summary
            emoji = "📌"  # Default emoji
            summary = event['summary'].lower()
            if any(word in summary for word in ['meeting', 'call', 'conference']):
                emoji = "💼"
            elif any(word in summary for word in ['lunch', 'dinner', 'breakfast']):
                emoji = "🍽️"
            elif any(word in summary for word in ['birthday', 'celebration', 'party']):
                emoji = "🎉"
            
            response.append(
                f"{emoji} {event['summary']} on {start_dt.strftime('%A, %B %d at %I:%M %p')}"
            )
            
        return "\n".join(response)

    async def cleanup(self):
        """Cleanup calendar tool resources"""
        try:
            if self.calendar_client:
                if hasattr(self.calendar_client, 'cleanup'):
                    await self.calendar_client.cleanup()
                    
            # Clear any cached data
            self.cache.clear()
            logger.info("Calendar tool cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during calendar tool cleanup: {e}") 