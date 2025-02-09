from enum import Enum
from typing import Dict, Optional, Any
from datetime import datetime, UTC
import logging
from src.db.db_schema import RinDB, ToolOperation
from src.utils.trigger_detector import TriggerDetector

logger = logging.getLogger(__name__)

class ToolOperationState(Enum):
    INACTIVE = "inactive"
    COLLECTING = "collecting"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"

class TweetStatus(Enum):
    """Status enum for tweet operations"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    FAILED = "failed"

class ToolStateManager:
    def __init__(self, db: RinDB, schedule_service=None):
        """Initialize tool state manager with database connection"""
        logger.info("Initializing ToolStateManager...")
        if not db:
            logger.error("Database instance is None!")
            raise ValueError("Database instance is required")
        if not isinstance(db, RinDB):
            logger.error(f"Expected RinDB instance, got {type(db)}")
            raise ValueError(f"Database must be RinDB instance, got {type(db)}")
        self.db = db
        self.schedule_service = schedule_service
        self.operations = {}
        self.trigger_detector = TriggerDetector()  # Initialize the trigger detector
        logger.info("ToolStateManager initialized with database connection")

    async def start_operation(self, 
                            session_id: str, 
                            operation_type: str,
                            initial_data: Optional[Dict[str, Any]] = None) -> bool:
        """Start a new tool operation"""
        try:
            operation_data = {
                "session_id": session_id,
                "state": ToolOperationState.COLLECTING.value,
                "operation_type": operation_type,
                "step": "initializing",
                "data": initial_data or {},
                "created_at": datetime.now(UTC),
                "last_updated": datetime.now(UTC)
            }
            
            success = await self.db.set_tool_operation_state(session_id, operation_data)
            if success:
                logger.info(f"Started operation {operation_type} for session {session_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error starting operation: {e}")
            return False

    async def update_operation(self,
                             session_id: str,
                             state: ToolOperationState,
                             step: str,
                             data: Optional[Dict] = None) -> bool:
        """Update an existing operation"""
        try:
            operation_data = {
                "state": state.value,
                "step": step,
                "last_updated": datetime.now(UTC)
            }
            
            if data:
                operation_data["data"] = {**await self.db.get_tool_operation_state(session_id).get("data", {}), **data}
                
            success = await self.db.set_tool_operation_state(session_id, operation_data)
            if success:
                logger.info(f"Updated operation state to {state} for session {session_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error updating operation: {e}")
            return False

    async def get_operation(self, session_id: str) -> Optional[ToolOperation]:
        """Get current operation state"""
        return await self.db.get_tool_operation_state(session_id)

    async def end_operation(self, session_id: str, success: bool = True) -> bool:
        """End an operation"""
        try:
            state = ToolOperationState.COMPLETED if success else ToolOperationState.ERROR
            operation_data = {
                "state": state.value,
                "step": "completed",
                "last_updated": datetime.now(UTC)
            }
            
            result = await self.db.set_tool_operation_state(session_id, operation_data)
            if result:
                logger.info(f"Ended operation for session {session_id} with state {state}")
            return result
            
        except Exception as e:
            logger.error(f"Error ending operation: {e}")
            return False

    async def get_operation_state(self, session_id: str) -> Optional[Dict]:
        """Get current operation state"""
        try:
            return await self.db.get_tool_operation_state(session_id)
        except Exception as e:
            logger.error(f"Error getting operation state: {e}")
            return None

    def should_use_tools(self, message: str) -> bool:
        """Check if message should trigger tool usage"""
        return self.trigger_detector.should_use_tools(message)

    def get_tool_operation_type(self, message: str) -> Optional[str]:
        """Get the tool operation type from the message"""
        return self.trigger_detector.get_tool_operation_type(message)

    async def execute_tool(self, tool_type: str, message: str):
        """Execute the appropriate tool based on type"""
        try:
            logger.info(f"Executing tool type: {tool_type} with message: {message}")
            
            if tool_type == "send_tweet" or tool_type == "schedule_tweets":
                # Twitter tool handling
                logger.info("Processing Twitter tool request")
                # Add your Twitter tool execution logic here
                return {"status": "success", "tool": "twitter", "action": tool_type}
            else:
                logger.warning(f"Unknown tool type: {tool_type}")
                return None
                
        except Exception as e:
            logger.error(f"Error executing tool: {e}", exc_info=True)
            return None 