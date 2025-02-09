from enum import Enum
from typing import Dict, Optional, Any
from datetime import datetime
import logging
from src.db.db_schema import RinDB, ToolOperation

logger = logging.getLogger(__name__)

class ToolOperationState(Enum):
    INACTIVE = "inactive"
    COLLECTING = "collecting"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"

class ToolStateManager:
    def __init__(self, db: RinDB):
        self.db = db

    async def start_operation(self, 
                            session_id: str, 
                            operation_type: str,
                            initial_data: Optional[Dict] = None) -> bool:
        """Start a new tool operation"""
        try:
            operation = ToolOperation(
                session_id=session_id,
                state=ToolOperationState.COLLECTING.value,
                operation_type=operation_type,
                step='initial',
                data=initial_data or {},
                created_at=datetime.utcnow(),
                last_updated=datetime.utcnow()
            )
            return await self.db.set_tool_operation_state(session_id, operation)
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
            operation = await self.db.get_tool_operation_state(session_id)
            if not operation:
                return False

            update_data = {
                "state": state.value,
                "step": step,
                "last_updated": datetime.utcnow()
            }
            if data:
                update_data["data"] = {**operation.get("data", {}), **data}

            return await self.db.set_tool_operation_state(session_id, update_data)
        except Exception as e:
            logger.error(f"Error updating operation: {e}")
            return False

    async def get_operation(self, session_id: str) -> Optional[ToolOperation]:
        """Get current operation state"""
        return await self.db.get_tool_operation_state(session_id)

    async def end_operation(self, session_id: str, success: bool = True) -> bool:
        """End an operation"""
        state = ToolOperationState.COMPLETED if success else ToolOperationState.ERROR
        return await self.update_operation(session_id, state, "completed") 