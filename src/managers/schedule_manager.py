from typing import Dict, List, Optional, Protocol
from datetime import datetime, UTC, timedelta
import logging
from src.db.db_schema import (
    RinDB,
    ToolOperationState,
    OperationStatus,
    ContentType
)
from src.managers.tool_state_manager import ToolStateManager
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)

class SchedulableToolProtocol(Protocol):
    async def execute_scheduled_operation(self, operation: Dict) -> Dict:
        ...

class ScheduleManager:
    def __init__(self, 
                 tool_state_manager: ToolStateManager, 
                 db: RinDB,
                 tool_registry: Dict[str, SchedulableToolProtocol]):
        self.tool_state_manager = tool_state_manager
        self.db = db
        self.tool_registry = tool_registry

    async def activate_schedule(
        self,
        tool_operation_id: str,
        schedule_info: Dict,
        content_type: str
    ) -> bool:
        """Activate a schedule by updating items with scheduled times"""
        try:
            # Get operation and items
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                logger.error(f"No operation found for ID {tool_operation_id}")
                return False

            items = await self.tool_state_manager.get_operation_items(tool_operation_id)
            if not items:
                logger.error(f"No items found for operation {tool_operation_id}")
                return False

            # Calculate scheduled times
            scheduled_times = self._calculate_schedule_times(
                schedule_info=schedule_info,
                item_count=len(items)
            )

            # Update each item with its scheduled time
            for item, scheduled_time in zip(items, scheduled_times):
                item["status"] = OperationStatus.SCHEDULED.value
                item["metadata"]["scheduled_time"] = scheduled_time.isoformat()
                
                await self.tool_state_manager.db.tool_items.update_one(
                    {"_id": item["_id"]},
                    {"$set": {
                        "status": OperationStatus.SCHEDULED.value,
                        "metadata.scheduled_time": scheduled_time.isoformat()
                    }}
                )

            # Let tool_state_manager determine operation state based on items
            await self.tool_state_manager.update_operation_state(
                tool_operation_id=tool_operation_id,
                item_updates=items  # Pass updated items to determine state
            )

            return True

        except Exception as e:
            logger.error(f"Error activating schedule: {e}")
            return False

    def _calculate_schedule_times(
        self,
        schedule_info: Dict,
        item_count: int
    ) -> List[datetime]:
        """Calculate schedule times for items"""
        # Always get current time at start of calculation
        current_time = datetime.now(UTC)
        
        start_time = schedule_info.get("start_time")
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        elif not start_time:
            # Add a small buffer to ensure we're always in the future
            start_time = current_time + timedelta(seconds=1)

        interval = timedelta(minutes=schedule_info.get("interval_minutes", 2))
        
        scheduled_times = [
            start_time + (interval * i)
            for i in range(item_count)
        ]
        
        # Verify all times are in the future
        if any(t <= current_time for t in scheduled_times):
            # If any time is not in the future, shift all times forward
            time_shift = (current_time - min(scheduled_times)) + timedelta(seconds=1)
            scheduled_times = [t + time_shift for t in scheduled_times]
        
        return scheduled_times

    async def check_schedule_completion(self, tool_operation_id: str) -> bool:
        """Check if all scheduled items are executed"""
        try:
            items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id
            }).to_list(None)

            all_executed = all(
                item["status"] == OperationStatus.EXECUTED.value
                for item in items
            )

            if all_executed:
                await self.tool_state_manager.update_operation_state(
                    tool_operation_id=tool_operation_id,
                    metadata={
                        "completion_time": datetime.now(UTC).isoformat()
                    }
                )

            return all_executed

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