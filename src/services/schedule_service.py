import asyncio
import logging
from datetime import datetime, UTC
from motor.motor_asyncio import AsyncIOMotorClient
from src.clients.twitter_client import TwitterAgentClient
from src.db.db_schema import ContentType, RinDB
from src.db.enums import OperationStatus, ToolOperationState, ToolType
from src.managers.tool_state_manager import ToolStateManager
from src.managers.schedule_manager import ScheduleManager, SchedulableToolProtocol
from src.tools.base import ToolRegistry
from bson.objectid import ObjectId
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

class ScheduleService:
    def __init__(self, mongo_uri: str, orchestrator=None):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = RinDB(self.mongo_client)
        self.tool_state_manager = ToolStateManager(db=self.db)
        
        # Initialize Twitter client first
        self.twitter_client = TwitterAgentClient()
        
        # Initialize tool registry with correct values
        self.tool_registry = ToolRegistry(
            content_type='tweet',   # ContentType.TWEET.value
            tool_type='twitter'     # ToolType.TWITTER.value
        )
        
        # Store tools separately for lookup
        self._tools = {
            'twitter': self.twitter_client
        }
        
        self.running = False
        self._task = None

    async def start(self):
        """Start the scheduling service"""
        if self.running:
            return
        
        await self.db.initialize()  # Initialize RinDB
        self.running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("Schedule service started")

    async def _schedule_loop(self):
        """Main scheduling loop that checks for and executes operations"""
        while self.running:
            try:
                # Get operations ready for execution
                due_operations = await self.db.get_scheduled_operation(
                    status=OperationStatus.SCHEDULED.value
                )
                
                # Ensure due_operations is a list
                if due_operations and not isinstance(due_operations, list):
                    due_operations = [due_operations]
                
                if due_operations:
                    for operation in due_operations:
                        if isinstance(operation, dict):  # Ensure operation is a dict
                            if operation.get('state') != ToolOperationState.CANCELLED.value:
                                try:
                                    await self._handle_scheduled_operation(operation)
                                except Exception as e:
                                    logger.error(f"Error processing operation {operation.get('_id')}: {e}")
                                    continue
                
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _handle_scheduled_operation(self, operation: Dict):
        """Handle a scheduled operation that's due for execution"""
        try:
            # Get the tool client for execution
            tool = self._get_tool_for_content(operation.get('content_type'))
            if not tool:
                logger.error(f"No tool found for content type: {operation.get('content_type')}")
                return

            # Get the scheduled items for this operation
            items = await self.db.tool_items.find({
                "tool_operation_id": operation['_id'],
                "status": OperationStatus.SCHEDULED.value
            }).to_list(None)

            for item in items:
                try:
                    scheduled_time = datetime.fromisoformat(
                        item.get('metadata', {}).get('scheduled_time').replace('Z', '+00:00')
                    )
                    
                    # Check if it's time to execute this item
                    if scheduled_time <= datetime.now(UTC):
                        # Execute the item
                        result = await tool.execute({
                            'content': {
                                'raw_content': item.get('content', {}).get('raw_content')
                            },
                            'parameters': {
                                'custom_params': {
                                    'account_id': item.get('parameters', {}).get('account_id', 'default'),
                                    'media_files': item.get('parameters', {}).get('media_files', []),
                                    'poll_options': item.get('parameters', {}).get('poll_options', []),
                                    'poll_duration': item.get('parameters', {}).get('poll_duration')
                                }
                            }
                        })

                        if result.get('success'):
                            # Update item status to executed
                            await self.db.tool_items.update_one(
                                {"_id": item['_id']},
                                {
                                    "$set": {
                                        "status": OperationStatus.EXECUTED.value,
                                        "metadata.execution_result": result,
                                        "metadata.executed_at": datetime.now(UTC).isoformat()
                                    }
                                }
                            )
                            logger.info(f"Successfully executed scheduled item {item['_id']}")
                        else:
                            await self._handle_execution_error(operation, f"Execution failed: {result.get('error')}")

                except Exception as e:
                    logger.error(f"Error executing scheduled item {item['_id']}: {e}")
                    continue

            # Check if all items are executed
            await self._check_schedule_completion(operation.get('metadata', {}).get('schedule_id'))

        except Exception as e:
            logger.error(f"Error handling scheduled operation: {e}")

    async def _execute_operation(self, operation: Dict):
        """Execute the operation using the appropriate tool"""
        try:
            # Get content type and execute appropriate tool
            content_type = operation.get('content_type')
            tool = self._get_tool_for_content(content_type)
            
            if not tool:
                logger.warning(f"No tool found for content type: {content_type}")
                return {'success': False, 'error': 'No tool found'}

            # Execute the operation using the appropriate tool
            result = await tool.execute_scheduled_operation(operation)
            
            return result

        except Exception as e:
            logger.error(f"Error executing operation: {e}")
            return {'success': False, 'error': str(e)}

    def _get_tool_for_content(self, content_type: str) -> Optional[Any]:
        """Get appropriate tool for content type"""
        try:
            if isinstance(content_type, ContentType):
                content_type = content_type.value
            
            if content_type == self.tool_registry.content_type:
                return self._tools.get(self.tool_registry.tool_type)
            return None
        except Exception as e:
            logger.error(f"Error getting tool for content type {content_type}: {e}")
            return None

    async def _check_schedule_completion(self, schedule_id: str):
        """Check if all operations in a schedule are complete"""
        try:
            # Use correct enum values
            schedule = await self.db.get_scheduled_operation(
                state=ToolOperationState.EXECUTING.value,  # Changed from OperationStatus
                status={"$ne": OperationStatus.EXECUTED.value}
            )
            
            if not schedule:
                # All operations are complete
                await self.db.update_schedule_state(
                    schedule_id=schedule_id,
                    state=OperationStatus.COMPLETED.value,
                    reason="All operations completed"
                )
                logger.info(f"Schedule {schedule_id} completed")

        except Exception as e:
            logger.error(f"Error checking schedule completion: {e}")

    async def _handle_execution_error(self, operation: Dict, error: str):
        """Handle execution error"""
        await self.tool_state_manager.update_operation(
            session_id=operation['session_id'],
            state=ToolOperationState.ERROR,
            content_status=OperationStatus.FAILED.value,
            error=error,
            content_updates={
                "last_error": error,
                "error_timestamp": datetime.now(UTC).isoformat()
            }
        )

    async def stop(self):
        """Stop the scheduling service"""
        if not self.running:
            return
            
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Schedule service stopped")

    def register_tool(self, tool_type: str, tool: SchedulableToolProtocol):
        """Register a tool that can be scheduled"""
        self.tool_registry[tool_type] = tool