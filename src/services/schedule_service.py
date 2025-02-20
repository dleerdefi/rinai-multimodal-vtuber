import asyncio
import logging
from datetime import datetime, UTC
from motor.motor_asyncio import AsyncIOMotorClient
from src.clients.twitter_client import TwitterAgentClient
from src.db.db_schema import OperationStatus, ContentType, RinDB, ToolOperationState
from src.managers.tool_state_manager import ToolStateManager
from bson.objectid import ObjectId
from typing import Dict

logger = logging.getLogger(__name__)

class ScheduleService:
    def __init__(self, mongo_uri: str, orchestrator=None):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = RinDB(self.mongo_client)
        self.tool_state_manager = ToolStateManager(db=self.db)
        self.orchestrator = orchestrator  # Store orchestrator reference
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
                due_operations = await self.db.get_scheduled_operations_for_execution(
                    content_type=ContentType.TWEET.value,
                    status=OperationStatus.SCHEDULED.value
                )
                
                due_operations = [op for op in due_operations 
                                if op.get('state') != ToolOperationState.CANCELLED.value]
                
                for operation in due_operations:
                    try:
                        await self._handle_scheduled_operation(operation)
                    except Exception as e:
                        logger.error(f"Error processing operation {operation['_id']}: {e}")
                        continue
                
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _handle_scheduled_operation(self, operation: Dict):
        """Handle scheduled operation execution"""
        try:
            # Verify operation is in PENDING state and approved
            if (operation.get('state') != ToolOperationState.PENDING.value or 
                operation.get('output_data', {}).get('status') != OperationStatus.APPROVED.value):
                return

            # Update to executing state
            await self.tool_state_manager.update_operation(
                session_id=operation['session_id'],
                state=ToolOperationState.EXECUTING,
                step="executing_schedule",
                content_status=OperationStatus.SCHEDULED,
                metadata={
                    "execution_started_at": datetime.now(UTC).isoformat()
                }
            )

            # Get appropriate tool and execute
            content_type = operation.get('metadata', {}).get('content_type')
            tool = self._get_tool_for_content(content_type)
            
            if not tool:
                raise ValueError(f"No tool found for content type: {content_type}")
            
            result = await tool.execute_scheduled_operation(operation)
            
            # Handle execution result
            if result.get('success'):
                await self.tool_state_manager.end_operation(
                    session_id=operation['session_id'],
                    success=True,
                    api_response=result,
                    reason="Schedule executed successfully"
                )
            else:
                await self._handle_execution_error(operation, result.get('error', 'Unknown error'))

        except Exception as e:
            logger.error(f"Error executing schedule: {e}")
            await self._handle_execution_error(operation, str(e))

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

    def _get_tool_for_content(self, content_type: str):
        """Get appropriate tool from orchestrator"""
        if not self.orchestrator:
            logger.error("No orchestrator available for tool lookup")
            return None
            
        # Map ContentType to tool name
        tool_name_map = {
            ContentType.TWEET.value: "twitter",
            # Add other content types here
        }
        
        tool_name = tool_name_map.get(content_type)
        if not tool_name:
            logger.warning(f"No tool mapping for content type: {content_type}")
            return None
            
        return self.orchestrator.tools.get(tool_name)

    async def _check_schedule_completion(self, schedule_id: str):
        """Check if all operations in a schedule are complete"""
        try:
            schedule = await self.db.get_scheduled_operations({
                "schedule_id": schedule_id,
                "status": {"$ne": OperationStatus.EXECUTED.value}
            })
            
            if not schedule:
                await self.db.update_schedule_status(
                    schedule_id=schedule_id,
                    status=OperationStatus.COMPLETED.value
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
