import logging
from typing import Dict, Any
from src.agents.rin.agent import RinAgent
from bson import json_util
import json
from datetime import datetime

logger = logging.getLogger(__name__)

class RinMessageHandler:
    def __init__(self, mongo_uri: str):
        """Initialize message handler with Rin agent."""
        self.mongo_uri = mongo_uri
        self.agent = RinAgent(mongo_uri=mongo_uri)
        
    async def initialize(self):
        """Initialize async components."""
        try:
            await self.agent.initialize()
            logger.info("RinMessageHandler initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize RinMessageHandler: {e}")
            # Handle GraphRAG initialization failure gracefully
            if "GraphRAG" in str(e):
                logger.warning("Continuing without GraphRAG functionality")
            else:
                raise
        
    async def handle_message(self, session_id: str, message: str) -> Dict[str, Any]:
        """Handle incoming messages with GraphRAG enhancement."""
        try:
            # Get response from agent
            response = await self.agent.get_response(session_id, message)
            
            return {
                "status": "success",
                "message": "Response generated",
                "data": {
                    "response": {
                        "status": "success",
                        "message": "Response generated successfully",
                        "data": {
                            "response": response,
                            "session_id": session_id
                        }
                    },
                    "sessionId": session_id
                }
            }
            
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            return {
                "status": "error",
                "message": "Failed to process message",
                "error": str(e)
            }
            
    async def start_session(self, session_id: str) -> Dict[str, Any]:
        """Start a new chat session with provided session ID."""
        try:
            welcome_message = await self.agent.start_new_session(session_id)
            
            return {
                "status": "success",
                "message": "Chat session initialized",
                "data": {
                    "session_id": session_id,
                    "welcome_message": "Konnichiwa!~ I'm Rin! Let's have a fun chat together! (＾▽＾)/"
                }
            }
            
        except Exception as e:
            logger.error(f"Error starting session: {e}", exc_info=True)
            return {
                "status": "error",
                "message": "Failed to start chat session",
                "error": str(e)
            }
            
    async def get_history(self, session_id: str):
        """Get chat history for a session."""
        try:
            history = await self.agent.get_history(session_id)
            history_serializable = json.loads(json_util.dumps(history))
            return {
                'status': 'success',
                'message': 'Chat history retrieved',
                'data': {
                    'session_id': session_id,
                    'history': history_serializable
                }
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': str(e)
            }