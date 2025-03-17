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
import time

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

        # Add monitoring service reference
        self.monitoring_service = None
        self.schedule_service = None

    async def inject_services(self, schedule_service=None, monitoring_service=None):
        """Inject service dependencies"""
        if schedule_service:
            self.schedule_service = schedule_service
        
        if monitoring_service:
            self.monitoring_service = monitoring_service

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
        error: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """Update item status after execution attempt"""
        try:
            update_data = {
                "status": status.value,
                "last_updated": datetime.now(UTC)
            }
            
            if status == OperationStatus.EXECUTED:
                update_data.update({
                    "state": ToolOperationState.COMPLETED.value,  # Update state to COMPLETED
                    "executed_time": datetime.now(UTC),
                    "api_response": api_response,
                    "metadata.schedule_state": ScheduleState.COMPLETED.value,
                    "metadata.execution_completed_at": datetime.now(UTC).isoformat()
                })
            
            if error:
                update_data["last_error"] = error
                update_data["retry_count"] = 1
                
            if metadata:
                update_data["metadata"] = {
                    **update_data.get("metadata", {}),
                    **metadata,
                    "last_modified": datetime.now(UTC).isoformat()
                }
            
            result = await self.db.tool_items.update_one(
                {"_id": ObjectId(item_id)},
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

            # Always create a new schedule
            logger.info(f"Creating new schedule for operation {tool_operation_id}")
            schedule_id = await self.db.create_scheduled_operation(
                tool_operation_id=tool_operation_id,
                content_type=content_type,
                schedule_info=schedule_info
            )

            # Initialize new schedule in PENDING state
            await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.INITIALIZE,
                reason="New schedule created for operation",
                metadata={
                    "tool_operation_id": tool_operation_id,
                    "content_type": content_type,
                    "schedule_info": schedule_info,
                    "created_at": datetime.now(UTC).isoformat()
                }
            )

            logger.info(f"Successfully created new schedule {schedule_id} for operation {tool_operation_id}")
            return schedule_id

        except Exception as e:
            logger.error(f"Error initializing schedule: {e}")
            raise

    async def activate_schedule(self, tool_operation_id: str, schedule_id: str) -> bool:
        """Activate a schedule with proper state validation"""
        try:
            # Get operation and verify state
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            
            # Get ALL tool items and let their states determine the operation state
            all_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=tool_operation_id,
                include_regenerated=True  # Include regenerated items
            )

            # Filter out rejected/cancelled items
            active_items = [
                item for item in all_items 
                if not item.get('metadata', {}).get('rejected_at')
                and not item.get('metadata', {}).get('cancelled_at')
            ]

            # Let tool_state_manager determine operation state based on active items
            await self.tool_state_manager.update_operation_state(
                tool_operation_id=tool_operation_id,
                item_updates=active_items
            )

            # Now get only the active/approved items for scheduling
            items_to_schedule = [
                item for item in active_items 
                if item.get('state') == ToolOperationState.EXECUTING.value
                and item.get('status') == OperationStatus.APPROVED.value
            ]
            
            logger.info(f"Found {len(items_to_schedule)} items to schedule")
            
            # Use existing monitoring params from items
            updated_monitoring_params = []
            for item in items_to_schedule:
                params = item.get('parameters', {}).get('custom_params', {})
                if params:
                    updated_monitoring_params.append(params)
                    logger.info(f"Using existing monitoring params for item {item['_id']}: {params}")
                else:
                    logger.error(f"Missing monitoring parameters for item {item['_id']}")
                    return False

            # Update schedule with current monitoring params and transition to ACTIVE state
            await self.db.update_schedule(
                schedule_id=schedule_id,
                update_data={
                    "schedule_state": ScheduleState.ACTIVE.value,  # Explicitly set ACTIVE state
                    "schedule_info": {
                        "monitoring_params_list": updated_monitoring_params,
                        "total_items": len(items_to_schedule)
                    },
                    "metadata": {
                        "active_items": [str(item['_id']) for item in items_to_schedule],
                        "last_updated": datetime.now(UTC).isoformat(),
                        "activation_time": datetime.now(UTC).isoformat(),
                        "state_transition": {
                            "from": ScheduleState.PENDING.value,
                            "to": ScheduleState.ACTIVE.value,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "reason": "Schedule activated with all items"
                        }
                    }
                }
            )

            # Schedule approved items
            start_time = datetime.now(UTC) + timedelta(minutes=1)
            for idx, item in enumerate(items_to_schedule, 1):
                scheduled_time = start_time + timedelta(minutes=idx-1)
                
                # Use the item's existing monitoring parameters
                monitoring_params = item.get('parameters', {}).get('custom_params', {})
                
                # Update item with schedule info and monitoring params
                await self.db.update_tool_item(
                    item_id=str(item['_id']),
                    update_data={
                        "state": ToolOperationState.COMPLETED.value,
                        "status": OperationStatus.SCHEDULED.value,
                        "scheduled_time": scheduled_time.isoformat(),
                        "execution_order": idx,
                        "metadata": {
                            **item.get('metadata', {}),  # Preserve existing metadata
                            "schedule_state": ScheduleState.ACTIVE.value,  # Match schedule state
                            "scheduling_type": "monitored",
                            "schedule_activation_time": datetime.now(UTC).isoformat(),
                            "schedule_state_history": [{
                                "state": ScheduleState.ACTIVE.value,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "reason": "Schedule activated"
                            }]
                        },
                        "parameters": {
                            "custom_params": monitoring_params
                        }
                    }
                )
                
                logger.info(f"Successfully scheduled item {item['_id']}")

            # After scheduling all items, update operation state to reflect scheduling
            await self.tool_state_manager.update_operation(
                session_id=operation.get('session_id'),
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.COMPLETED.value,  # Set to COMPLETED since scheduling is done
                status=OperationStatus.SCHEDULED.value,
                metadata={
                    "schedule_state": ScheduleState.ACTIVE.value,
                    "schedule_activation_time": datetime.now(UTC).isoformat(),
                    "total_scheduled_items": len(items_to_schedule)
                }
            )

            # Then transition schedule state using the state machine
            await self._transition_schedule_state(
                schedule_id=schedule_id,
                action=ScheduleAction.ACTIVATE,
                reason="Schedule activated with all items",
                metadata={
                    "activated_at": datetime.now(UTC).isoformat(),
                    "total_items": len(items_to_schedule),
                    "item_ids": [str(item['_id']) for item in items_to_schedule]
                }
            )

            logger.info(f"Successfully activated schedule {schedule_id} with {len(items_to_schedule)} items")
            return True
            
        except Exception as e:
            logger.error(f"Error activating schedule: {e}")
            return False

    def _calculate_valid_start_time(self, start_time_str: Optional[str], current_time: datetime) -> datetime:
        """Calculate valid start time with proper validation"""
        if start_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                if start_time <= current_time:
                    logger.warning(f"Start time {start_time} is in the past, adjusting to future")
                    start_time = current_time + timedelta(minutes=1)
            except (ValueError, TypeError):
                logger.warning(f"Invalid start time format: {start_time_str}, using current time + 1 minute")
                start_time = current_time + timedelta(minutes=1)
        else:
            start_time = current_time + timedelta(minutes=1)
        
        return start_time

    def _create_monitoring_params(self, item: Dict) -> Dict:
        """Create monitoring parameters with validation"""
        operation_details = item.get("content", {}).get("operation_details", {})
        
        monitoring_params = {
            "check_interval_seconds": 60,
            "last_checked_timestamp": int(datetime.now(UTC).timestamp()),
            "best_price_seen": 0,
            "expiration_timestamp": int(datetime.now(UTC).timestamp()) + 86400,  # 24 hours
            "max_checks": 1000,
            "reference_token": operation_details.get("reference_token"),
            "target_price_usd": float(operation_details.get("target_price_usd", 0)),
            "from_token": operation_details.get("from_token"),
            "from_amount": float(operation_details.get("from_amount", 0)),
            "to_token": operation_details.get("to_token", "ethereum")
        }

        # Validate required fields
        required_fields = ["reference_token", "target_price_usd", "from_token", "from_amount", "to_token"]
        missing_fields = [f for f in required_fields if not monitoring_params.get(f)]
        if missing_fields:
            logger.error(f"Missing required monitoring fields: {missing_fields}")
            return None

        return monitoring_params

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
            schedule = await self.db.get_scheduled_operation(schedule_id)
            if not schedule:
                return False

            # Get ONLY active items (not rejected/cancelled)
            all_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=schedule['tool_operation_id'],
                include_regenerated=True
            )
            active_items = [
                item for item in all_items 
                if not item.get('metadata', {}).get('rejected_at')
                and not item.get('metadata', {}).get('cancelled_at')
            ]

            # Verify ONLY active items are in appropriate states
            items_ready = self._verify_items_ready_for_transition(active_items, action)
            if not items_ready:
                logger.error("Items not in appropriate states for schedule transition")
                return False

            current_state = ScheduleState(schedule.get("schedule_state", ScheduleState.PENDING.value))
            next_state = self.state_transitions.get((current_state, action))

            if not next_state:
                logger.error(f"Invalid state transition: {current_state} -> {action}")
                return False

            # Create history entry
            history_entry = {
                "state": next_state.value,
                "timestamp": datetime.now(UTC).isoformat(),
                "reason": f"{action.value}: {reason}"
            }

            # Update state using db_schema method with proper array handling
            update_ops = {
                "$set": {
                    "schedule_state": next_state.value,
                    "last_updated": datetime.now(UTC)
                },
                "$push": {
                    "state_history": history_entry
                }
            }
            
            # Add metadata if provided
            if metadata:
                update_ops["$set"]["metadata"] = {
                    **schedule.get("metadata", {}),
                    **metadata,
                    "last_modified": datetime.now(UTC).isoformat()
                }
            
            result = await self.db.scheduled_operations.update_one(
                {"_id": ObjectId(schedule_id)},
                update_ops
            )
            
            success = result.modified_count > 0
            if success:
                logger.info(f"Updated schedule {schedule_id} state to {next_state.value}")
            else:
                logger.warning(f"No schedule updated for ID: {schedule_id}")
            
            return success

        except Exception as e:
            logger.error(f"Error in state transition: {e}", exc_info=True)
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
                "status": {"$ne": OperationStatus.REJECTED.value}  # Exclude rejected items
            }).to_list(None)

            if not items:
                logger.warning(f"No items found for schedule {schedule_id}")
                return False

            # Count items by schedule state
            items_by_state = {
                'pending': [i for i in items if i.get('metadata', {}).get('schedule_state') == ScheduleState.PENDING.value],
                'active': [i for i in items if i.get('metadata', {}).get('schedule_state') == ScheduleState.ACTIVE.value],
                'completed': [i for i in items if i.get('metadata', {}).get('schedule_state') == ScheduleState.COMPLETED.value],
                'error': [i for i in items if i.get('metadata', {}).get('schedule_state') == ScheduleState.ERROR.value],
                'cancelled': [i for i in items if i.get('metadata', {}).get('schedule_state') == ScheduleState.CANCELLED.value]
            }

            # Check if all non-rejected items have reached a terminal state
            all_terminal = all(
                i.get('metadata', {}).get('schedule_state') in [
                    ScheduleState.COMPLETED.value,
                    ScheduleState.ERROR.value,
                    ScheduleState.CANCELLED.value
                ]
                for i in items
            )

            if all_terminal:
                # Update schedule state to COMPLETED
                await self._transition_schedule_state(
                    schedule_id=schedule_id,
                    action=ScheduleAction.COMPLETE,
                    reason="All scheduled items reached terminal state",
                    metadata={
                        "completion_time": datetime.now(UTC).isoformat(),
                        "total_items": len(items),
                        "execution_summary": {
                            "completed": len(items_by_state['completed']),
                            "error": len(items_by_state['error']),
                            "cancelled": len(items_by_state['cancelled'])
                        }
                    }
                )

            return all_terminal

        except Exception as e:
            logger.error(f"Error checking schedule completion: {e}")
            return False

    async def execute_operation(self, operation: Dict) -> Dict:
        """Execute operation using appropriate tool from registry"""
        try:
            # Get content type and tool
            content_type = operation.get('content_type')
            tool = self.tool_registry.get(content_type)
            
            if not tool:
                raise ValueError(f"No tool found for content type: {content_type}")

            # Log execution attempt
            logger.info(f"Executing {content_type} operation {operation.get('_id')} using {type(tool).__name__}")

            # Execute using tool's execute_scheduled_operation method
            result = await tool.execute_scheduled_operation(operation)
            
            # Update operation status based on result
            if result.get('success'):
                # All steps completed successfully
                await self.update_item_execution_status(
                    item_id=str(operation['_id']),
                    status=OperationStatus.EXECUTED,
                    api_response=result,
                    metadata={
                        "execution_completed_at": result['execution_time'],
                        "execution_steps": result['execution_steps'],
                        "final_result": result['final_result']
                    }
                )
            else:
                # Handle failure with details
                await self.update_item_execution_status(
                    item_id=str(operation['_id']),
                    status=OperationStatus.FAILED,
                    error=result.get('error', 'Unknown error'),
                    metadata={
                        "failed_at": result['execution_time'],
                        "execution_steps": result['execution_steps']
                    }
                )
            
            return result

        except Exception as e:
            logger.error(f"Error executing operation: {e}")
            # Update operation with error status
            await self.update_item_execution_status(
                item_id=str(operation['_id']),
                status=OperationStatus.FAILED,
                error=str(e)
            )
            return {'success': False, 'error': str(e)} 

    async def link_regenerated_items(
        self,
        tool_operation_id: str,
        schedule_id: str,
        new_items: List[Dict]
    ) -> bool:
        """Ensure regenerated items maintain proper links to parent operation and schedule"""
        try:
            logger.info(f"Linking {len(new_items)} regenerated items to schedule {schedule_id}")
            
            # First verify the schedule exists and is linked to the operation
            schedule = await self.db.get_scheduled_operation(schedule_id)
            if not schedule:
                logger.error(f"Schedule {schedule_id} not found")
                return False
            
            if schedule.get('tool_operation_id') != tool_operation_id:
                logger.error(f"Schedule {schedule_id} does not belong to operation {tool_operation_id}")
                return False
            
            logger.info(f"Found valid schedule: state={schedule.get('schedule_state')}")

            # Update each regenerated item with schedule linkage
            for item in new_items:
                await self.db.update_tool_item(
                    item_id=str(item['_id']),
                    update_data={
                        "schedule_id": schedule_id,
                        "tool_operation_id": tool_operation_id,  # Ensure parent operation link
                        "metadata": {
                            **item.get('metadata', {}),
                            "schedule_linkage": {
                                "linked_at": datetime.now(UTC).isoformat(),
                                "schedule_id": schedule_id,
                                "schedule_state": schedule.get('schedule_state'),
                                "is_regenerated": True
                            }
                        }
                    }
                )
                logger.info(f"Linked item {item['_id']} to schedule {schedule_id}")

            # Update schedule to track regenerated items
            await self.db.update_schedule(
                schedule_id=schedule_id,
                update_data={
                    "metadata": {
                        "regenerated_items": [str(item['_id']) for item in new_items],
                        "last_regeneration": datetime.now(UTC).isoformat()
                    },
                    "pending_items": [
                        *schedule.get('pending_items', []),
                        *[str(item['_id']) for item in new_items]
                    ]
                }
            )
            logger.info(f"Updated schedule {schedule_id} with regenerated items")

            return True

        except Exception as e:
            logger.error(f"Error linking regenerated items: {e}", exc_info=True)
            return False 

    def _verify_items_ready_for_transition(self, items: List[Dict], action: ScheduleAction) -> bool:
        """Verify items are in appropriate states for schedule transition"""
        if action == ScheduleAction.ACTIVATE:
            # All items should be EXECUTING/APPROVED
            return all(
                item.get('state') == ToolOperationState.EXECUTING.value
                and item.get('status') == OperationStatus.APPROVED.value
                for item in items
            )
        elif action == ScheduleAction.COMPLETE:
            # All items should be COMPLETED/EXECUTED
            return all(
                item.get('state') == ToolOperationState.COMPLETED.value
                and item.get('status') == OperationStatus.EXECUTED.value
                for item in items
            )
        # Add other action validations as needed
        return True 