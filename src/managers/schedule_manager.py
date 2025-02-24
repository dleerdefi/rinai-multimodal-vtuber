from typing import Dict, List, Optional, Protocol
from datetime import datetime, UTC, timedelta
import logging
from src.db.db_schema import (
    RinDB,
    ContentType,
    ToolItem,
)
from src.db.enums import OperationStatus, ToolOperationState, ScheduleState
from src.managers.tool_state_manager import ToolStateManager
from bson.objectid import ObjectId
from enum import Enum
from src.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

class SchedulableToolProtocol(Protocol):
    async def execute_scheduled_operation(self, operation: Dict) -> Dict:
        ...

class ScheduleAction(Enum):
    """Actions that trigger schedule state transitions"""
    INITIALIZE = "initialize"     # PENDING -> PENDING
    ACTIVATE = "activate"        # PENDING -> ACTIVATING -> ACTIVE
    PAUSE = "pause"             # ACTIVE -> PAUSED
    RESUME = "resume"           # PAUSED -> ACTIVE
    CANCEL = "cancel"           # Any -> CANCELLED
    ERROR = "error"             # Any -> ERROR
    COMPLETE = "complete"        # EXECUTING -> COMPLETED

class ScheduleManager:
    def __init__(self, 
                 tool_state_manager: ToolStateManager, 
                 db: RinDB,
                 tool_registry: Dict[str, SchedulableToolProtocol]):
        self.tool_state_manager = tool_state_manager
        self.db = db
        self.tool_registry = tool_registry
        
        # Define valid state transitions for Schedule
        self.state_transitions = {
            (ScheduleState.PENDING, ScheduleAction.INITIALIZE): ScheduleState.PENDING,
            (ScheduleState.PENDING, ScheduleAction.ACTIVATE): ScheduleState.ACTIVATING,
            (ScheduleState.ACTIVATING, ScheduleAction.ACTIVATE): ScheduleState.ACTIVE,
            (ScheduleState.ACTIVE, ScheduleAction.PAUSE): ScheduleState.PAUSED,
            (ScheduleState.PAUSED, ScheduleAction.RESUME): ScheduleState.ACTIVE,
            # Any state can transition to CANCELLED or ERROR
            (ScheduleState.ACTIVE, ScheduleAction.CANCEL): ScheduleState.CANCELLED,
            (ScheduleState.PAUSED, ScheduleAction.CANCEL): ScheduleState.CANCELLED,
            (ScheduleState.ACTIVE, ScheduleAction.ERROR): ScheduleState.ERROR,
            (ScheduleState.ERROR, ScheduleAction.RESUME): ScheduleState.ACTIVE,
            (ScheduleState.ACTIVE, ScheduleAction.COMPLETE): ScheduleState.COMPLETED
        }

    async def schedule_approved_items(
        self,
        tool_operation_id: str,
        schedule_info: Dict,
    ) -> bool:
        """Schedule approved items based on stored scheduling parameters"""
        try:
            # Get operation and approved items
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                logger.error(f"No operation found for ID {tool_operation_id}")
                return False

            approved_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id,
                status=OperationStatus.APPROVED.value
            )
            
            if not approved_items:
                logger.error(f"No approved items found for operation {tool_operation_id}")
                return False

            # Calculate schedule times for approved items
            scheduled_times = self._calculate_schedule_times(
                schedule_info=schedule_info,
                item_count=len(approved_items)
            )

            # Update each approved item with scheduled status and time
            for item, scheduled_time in zip(approved_items, scheduled_times):
                await self.db.tool_items.update_one(
                    {"_id": item["_id"]},
                    {"$set": {
                        "status": OperationStatus.SCHEDULED.value,
                        "scheduled_time": scheduled_time,
                        "metadata.scheduled_at": datetime.now(UTC).isoformat(),
                        "last_updated": datetime.now(UTC)
                    }}
                )

            # Update operation state to indicate scheduling is complete
            await self.tool_state_manager.update_operation_state(
                tool_operation_id=tool_operation_id,
                state=OperationStatus.SCHEDULED.value,
                metadata={
                    "scheduled_at": datetime.now(UTC).isoformat(),
                    "schedule_info": schedule_info
                }
            )

            return True

        except Exception as e:
            logger.error(f"Error scheduling approved items: {e}")
            return False

    def _calculate_schedule_times(
        self,
        schedule_info: Dict,
        item_count: int
    ) -> List[datetime]:
        """Calculate schedule times for items based on scheduling parameters"""
        current_time = datetime.now(UTC)
        
        # Parse start time
        start_time = schedule_info.get("start_time")
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        elif not start_time:
            start_time = current_time + timedelta(seconds=1)

        # Get interval (default 2 minutes)
        interval = timedelta(minutes=schedule_info.get("interval_minutes", 2))
        
        # Calculate times for each item
        scheduled_times = [
            start_time + (interval * i)
            for i in range(item_count)
        ]
        
        # Ensure all times are in the future
        if any(t <= current_time for t in scheduled_times):
            time_shift = (current_time - min(scheduled_times)) + timedelta(seconds=1)
            scheduled_times = [t + time_shift for t in scheduled_times]
        
        return scheduled_times

    async def get_scheduled_items(
        self,
        content_type: Optional[str] = None,
        before_time: Optional[datetime] = None
    ) -> List[Dict]:
        """Get scheduled items, optionally filtered by type and time"""
        try:
            query = {"status": OperationStatus.SCHEDULED.value}
            
            if content_type:
                query["content_type"] = content_type
                
            if before_time:
                query["scheduled_time"] = {"$lte": before_time}
            
            cursor = self.db.tool_items.find(query)
            return await cursor.to_list(length=None)
            
        except Exception as e:
            logger.error(f"Error fetching scheduled items: {e}")
            return []

    async def update_item_execution_status(
        self,
        item_id: str,
        status: OperationStatus,
        api_response: Optional[Dict] = None,
        error: Optional[str] = None
    ) -> bool:
        """Update item status after execution attempt"""
        try:
            update_data = {
                "status": status.value,
                "last_updated": datetime.now(UTC)
            }
            
            if status == OperationStatus.EXECUTED:
                update_data.update({
                    "executed_time": datetime.now(UTC),
                    "api_response": api_response
                })
            
            if error:
                update_data["last_error"] = error
                update_data["retry_count"] = 1
            
            result = await self.db.tool_items.update_one(
                {"_id": item_id},
                {"$set": update_data}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error updating item execution status: {e}")
            return False

    async def initialize_schedule(
        self,
        tool_operation_id: str,
        schedule_info: Dict,
        content_type: str,
        session_id: Optional[str] = None
    ) -> str:
        """Initialize a new schedule in PENDING state with state tracking"""
        try:
            # Get operation to retrieve session_id if not provided
            if not session_id:
                operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
                if not operation:
                    raise ValueError(f"No operation found for ID {tool_operation_id}")
                session_id = operation.get("session_id")
                if not session_id:
                    raise ValueError(f"No session_id found for operation {tool_operation_id}")

            # Create schedule using db_schema's method
            schedule_id = await self.db.create_scheduled_operation(
                tool_operation_id=tool_operation_id,
                content_type=content_type,
                schedule_info=schedule_info
            )

            # Track state transition
            await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.INITIALIZE,
                reason="Schedule initialized with pending items",
                metadata={
                    "tool_operation_id": tool_operation_id,
                    "content_type": content_type,
                    "schedule_info": schedule_info
                }
            )

            # Update tool operation with schedule reference
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "schedule_id": schedule_id,
                    "schedule_state": ScheduleState.PENDING.value
                }
            )

            return schedule_id

        except Exception as e:
            logger.error(f"Error initializing schedule: {e}")
            raise

    async def activate_schedule(
        self,
        tool_operation_id: str,
        schedule_id: str
    ) -> bool:
        """Activate a schedule after items are ready for execution"""
        try:
            # 1. Verify items are in EXECUTING state
            items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.EXECUTING.value,  # Items must be in EXECUTING state
                status=[                                   # Status can be either:
                    OperationStatus.APPROVED.value,        # - APPROVED (from approval flow)
                    OperationStatus.PENDING.value          # - PENDING (from direct/one-shot flow)
                ]
            )
            
            if not items:
                logger.error(f"No executable items found for operation {tool_operation_id}")
                return False

            # 2. Update schedule state only
            await self.db.update_scheduled_operation(
                schedule_id=schedule_id,
                state=ScheduleState.ACTIVE.value,
                status=OperationStatus.SCHEDULED.value,
                metadata={
                    "state_history": {
                        "schedule_state": ScheduleState.ACTIVE.value,
                        "reason": "Schedule activated for execution",
                        "timestamp": datetime.now(UTC).isoformat()
                    },
                    "execution_status": {
                        "pending": len(items),
                        "completed": 0,
                        "failed": 0
                    }
                }
            )

            # 3. Update only item status (state remains EXECUTING)
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "_id": {"$in": [ObjectId(item["_id"]) for item in items]}
                },
                {"$set": {"status": OperationStatus.SCHEDULED.value}}
            )

            return True

        except Exception as e:
            logger.error(f"Error activating schedule: {e}")
            return False

    async def pause_schedule(self, schedule_id: str) -> bool:
        """Pause an active schedule"""
        try:
            return await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.PAUSE,
                reason="Schedule paused by request"
            )
        except Exception as e:
            logger.error(f"Error pausing schedule: {e}")
            return False

    async def resume_schedule(self, schedule_id: str) -> bool:
        """Resume a paused schedule"""
        try:
            return await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.RESUME,
                reason="Schedule resumed by request"
            )
        except Exception as e:
            logger.error(f"Error resuming schedule: {e}")
            return False

    async def cancel_schedule(self, schedule_id: str) -> bool:
        """Cancel a schedule"""
        try:
            # Get current schedule
            schedule = await self.db.get_scheduled_operation(schedule_id)
            if not schedule:
                logger.error(f"No schedule found for ID: {schedule_id}")
                return False

            # Cancel all pending items
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": schedule["tool_operation_id"],
                    "status": OperationStatus.SCHEDULED.value
                },
                {"$set": {
                    "status": OperationStatus.REJECTED.value,
                    "state": ToolOperationState.CANCELLED.value,
                    "metadata.cancelled_at": datetime.now(UTC).isoformat(),
                    "metadata.cancel_reason": "Schedule cancelled"
                }}
            )

            # Update schedule state
            return await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.CANCEL,
                reason="Schedule cancelled by request",
                metadata={"cancelled_at": datetime.now(UTC).isoformat()}
            )

        except Exception as e:
            logger.error(f"Error cancelling schedule: {e}")
            return False

    async def _transition_schedule_state(
        self,
        schedule_id: str,
        action: ScheduleAction,
        reason: str,
        metadata: Optional[Dict] = None
    ) -> bool:
        """Handle schedule state transitions with validation and history tracking"""
        try:
            # Get current state
            schedule = await self.db.get_scheduled_operation(schedule_id)
            if not schedule:
                logger.error(f"No schedule found for ID: {schedule_id}")
                return False

            current_state = ScheduleState(schedule.get("schedule_state", ScheduleState.PENDING.value))
            next_state = self.state_transitions.get((current_state, action))

            if not next_state:
                logger.error(f"Invalid state transition: {current_state} -> {action}")
                return False

            # Update state using db_schema method
            return await self.db.update_schedule_state(
                schedule_id=schedule_id,
                state=next_state,
                reason=f"{action.value}: {reason}",
                metadata=metadata
            )

        except Exception as e:
            logger.error(f"Error in state transition: {e}")
            return False

    async def check_schedule_completion(self, schedule_id: str) -> bool:
        """Check if all items in scheduled operation are completed"""
        try:
            schedule = await self.db.get_scheduled_operation(schedule_id)
            if not schedule:
                return False

            # Get all items for this schedule
            items = await self.db.tool_items.find({
                "schedule_id": schedule_id,
                "state": ToolOperationState.COMPLETED.value  # Items should be in EXECUTING state
            }).to_list(None)

            if not items:
                logger.warning(f"No items found for schedule {schedule_id}")
                return False

            # Check if all items have completed their schedule
            all_completed = all(
                item.get("schedule_state") == ScheduleState.COMPLETED.value
                for item in items
            )

            if all_completed:
                # Update schedule state to COMPLETED
                await self._transition_schedule_state(
                    schedule_id=schedule_id,
                    action=ScheduleAction.COMPLETE,
                    reason="All scheduled items completed execution",
                    metadata={
                        "completion_time": datetime.now(UTC).isoformat(),
                        "total_items": len(items),
                        "execution_summary": {
                            "completed": len([i for i in items if i.get("schedule_state") == ScheduleState.COMPLETED.value]),
                            "error": len([i for i in items if i.get("schedule_state") == ScheduleState.ERROR.value]),
                            "cancelled": len([i for i in items if i.get("schedule_state") == ScheduleState.CANCELLED.value])
                        }
                    }
                )

            return all_completed

        except Exception as e:
            logger.error(f"Error checking schedule completion: {e}")
            return False

    async def execute_operation(self, operation: Dict) -> Dict:
        """Execute operation using appropriate tool from registry"""
        try:
            content_type = operation.get('metadata', {}).get('content_type')
            tool = self.tool_registry.get(content_type)
            
            if not tool:
                raise ValueError(f"No tool found for content type: {content_type}")

            result = await tool.execute_scheduled_operation(operation)
            
            if result.get('success'):
                await self._update_execution_status(operation['_id'], result)
            else:
                await self._handle_execution_error(operation['_id'], result.get('error'))

            return result

        except Exception as e:
            logger.error(f"Error executing operation: {e}")
            await self._handle_execution_error(operation['_id'], str(e))
            return {'success': False, 'error': str(e)} 