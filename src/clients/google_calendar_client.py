# Standard library imports
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict

# Third-party imports
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pickle

# Initialize logger
logger = logging.getLogger(__name__)

class GoogleCalendarClient:
    """Client for Google Calendar API interactions"""
    
    def __init__(self):
        self.SCOPES = ['https://www.googleapis.com/auth/calendar']  # Full access scope
        self.creds = None
        self.service = None
        
    async def initialize(self):
        """Initialize the calendar client with proper authentication"""
        try:
            # Load existing credentials if available
            try:
                with open('token.pickle', 'rb') as token:
                    self.creds = pickle.load(token)
            except:
                self.creds = None

            # If no valid credentials, get new ones
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', self.SCOPES)
                    self.creds = flow.run_local_server(port=0)

                # Save credentials for future use
                with open('token.pickle', 'wb') as token:
                    pickle.dump(self.creds, token)

            # Build the service with cache_discovery=False to suppress the warning
            self.service = build('calendar', 'v3', credentials=self.creds, cache_discovery=False)
            return True

        except Exception as e:
            logger.error(f"Error initializing calendar client: {e}")
            return False
    
    async def get_upcoming_events(self, maxResults: int = 10, time_min: Optional[str] = None, time_max: Optional[str] = None) -> List[Dict]:
        """Get upcoming calendar events
        
        Args:
            maxResults: Maximum number of events to return
            time_min: Start time in ISO format (optional)
            time_max: End time in ISO format (optional)
            
        Returns:
            List[Dict]: List of calendar events
        """
        try:
            if not self.service:
                raise Exception("Client not initialized")
                
            # Get current time if time_min not provided
            if time_min is None:
                time_min = datetime.utcnow().isoformat() + 'Z'
            
            # Build request parameters
            params = {
                'calendarId': 'primary',
                'timeMin': time_min,
                'maxResults': maxResults,
                'singleEvents': True,
                'orderBy': 'startTime'
            }
            
            # Add optional time_max if provided
            if time_max:
                params['timeMax'] = time_max
            
            events_result = self.service.events().list(**params).execute()
            events = events_result.get('items', [])
            return events
            
        except Exception as e:
            logger.error(f"Error fetching calendar events: {e}")
            return []
    
    async def create_event(self, event_data: Dict) -> Dict:
        """Create a new calendar event
        
        Args:
            event_data: Dictionary containing event details
            
        Returns:
            Dict: Created event data
        """
        try:
            if not self.service:
                raise ValueError("Calendar service not initialized")
                
            event = self.service.events().insert(
                calendarId='primary',
                body=event_data,
                sendUpdates='all'  # Notify attendees
            ).execute()
            
            return event
            
        except Exception as e:
            logger.error(f"Error creating event: {e}")
            raise 