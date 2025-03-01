import asyncio
import logging
from datetime import datetime, UTC
from motor.motor_asyncio import AsyncIOMotorClient
from src.clients.twitter_client import TwitterAgentClient
from src.db.db_schema import ContentType, RinDB
from src.db.enums import OperationStatus, ToolOperationState, ScheduleState
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
        
        # Store tools separately for lookup
        self._tools = {
            ContentType.TWEET.value: self.twitter_client,
            'tweet': self.twitter_client  # Add string version for flexibility
        }
        
        # Create a tool registry for the schedule manager
        tool_registry = {
            ContentType.TWEET.value: self.twitter_client,
            'tweet': self.twitter_client  # Add string version for flexibility
        }
        
        # Initialize schedule manager with the tool registry
        self.schedule_manager = ScheduleManager(
            tool_state_manager=self.tool_state_manager,
            db=self.db,
            tool_registry=tool_registry
        )
        
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
                # Get current time
                current_time = datetime.now(UTC)
                
                # Get all scheduled items due for execution
                due_items = await self.db.tool_items.find({
                    "status": OperationStatus.SCHEDULED.value,
                    "scheduled_time": {"$lte": current_time}
                }).to_list(None)
                
                if due_items:
                    logger.info(f"Found {len(due_items)} items due for execution at {current_time.isoformat()}")
                    
                    for item in due_items:
                        try:
                            # Get the tool for this content type
                            content_type = item.get('content_type')
                            tool = self._get_tool_for_content(content_type)
                            
                            if not tool:
                                logger.error(f"No tool found for content type: {content_type}")
                                continue
                            
                            # Execute the item
                            logger.info(f"Executing scheduled item: {item.get('_id')}")
                            
                            # Try different execution methods in order of preference
                            result = None
                            execution_error = None
                            
                            try:
                                # 1. Try execute_scheduled_operation if it exists
                                if hasattr(tool, 'execute_scheduled_operation'):
                                    result = await tool.execute_scheduled_operation(item)
                                # 2. Fall back to execute method if available
                                elif hasattr(tool, 'execute'):
                                    result = await tool.execute(item)
                                # 3. Use send_tweet directly for TwitterAgentClient as last resort
                                elif hasattr(tool, 'send_tweet') and content_type == ContentType.TWEET.value:
                                    content = item.get('content', {}).get('raw_content')
                                    if not content:
                                        content = item.get('content', {}).get('formatted_content')
                                    
                                    if not content:
                                        raise ValueError("No content found for scheduled tweet")
                                    
                                    params = {
                                        'account_id': item.get('metadata', {}).get('account_id', 'default'),
                                        'media_files': item.get('metadata', {}).get('media_files', []),
                                        'poll_options': item.get('metadata', {}).get('poll_options', [])
                                    }
                                    
                                    tweet_result = await tool.send_tweet(content=content, params=params)
                                    result = {
                                        'success': tweet_result.get('success', False),
                                        'id': tweet_result.get('id'),
                                        'text': content,
                                        'timestamp': datetime.now(UTC).isoformat(),
                                        'result': tweet_result
                                    }
                                else:
                                    raise ValueError(f"Tool {type(tool).__name__} has no suitable execution method")
                            except Exception as exec_error:
                                execution_error = exec_error
                                logger.error(f"Error executing item: {exec_error}")
                                result = {'success': False, 'error': str(exec_error)}
                            
                            # Update item status based on execution result
                            if result and result.get('success'):
                                await self.db.tool_items.update_one(
                                    {"_id": item['_id']},
                                    {"$set": {
                                        "status": OperationStatus.EXECUTED.value,
                                        "state": ToolOperationState.COMPLETED.value,
                                        "executed_time": datetime.now(UTC),
                                        "api_response": result,
                                        "metadata.execution_result": result,
                                        "metadata.executed_at": datetime.now(UTC).isoformat(),
                                        "metadata.schedule_state": ScheduleState.COMPLETED.value
                                    }}
                                )
                                logger.info(f"Successfully executed scheduled item {item['_id']}")
                                
                                # Update schedule execution status
                                if item.get('schedule_id'):
                                    await self.db.scheduled_operations.update_one(
                                        {"_id": ObjectId(item.get('schedule_id'))},
                                        {"$inc": {
                                            "metadata.execution_status.pending": -1,
                                            "metadata.execution_status.completed": 1
                                        }}
                                    )
                                    
                                    # Check if schedule is complete
                                    await self._check_schedule_completion(item.get('schedule_id'))
                            else:
                                error_msg = result.get('error') if result else str(execution_error)
                                logger.error(f"Failed to execute scheduled item {item['_id']}: {error_msg}")
                        
                        except Exception as e:
                            logger.error(f"Error executing scheduled item {item.get('_id')}: {e}")
                
                # Check every 10 seconds
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")
                await asyncio.sleep(10)  # Wait before retry

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
            
            # Normalize content type string
            if content_type == ContentType.TWEET.value or content_type == 'tweet':
                # Return the twitter client
                return self.twitter_client
            
            # For other content types, check the registry
            return self._tools.get(content_type)
        except Exception as e:
            logger.error(f"Error getting tool for content type {content_type}: {e}")
            return None

    async def _check_schedule_completion(self, schedule_id: str):
        """Check if all operations in a schedule are complete"""
        try:
            # Use the schedule manager to check completion
            await self.schedule_manager.check_schedule_completion(schedule_id)
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