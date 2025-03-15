from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Optional, Any, Type
import logging
import asyncio
import os
from dotenv import load_dotenv
from datetime import datetime, UTC, timedelta
import json
from bson import ObjectId

# Base imports
from src.tools.base import (
    BaseTool,
    AgentResult, 
    AgentDependencies,
    ToolCommand,
    CommandAnalysis,
    TimeToolParameters,
    WeatherToolParameters,
    CryptoToolParameters,
    SearchToolParameters,
    CalendarToolParameters,
    ToolRegistry
)

# Tool imports - only import TwitterTool for testing
from src.tools.post_tweets import TwitterTool

# Client imports
from src.clients.coingecko_client import CoinGeckoClient
from src.clients.perplexity_client import PerplexityClient
from src.clients.google_calendar_client import GoogleCalendarClient
from src.clients.near_account_helper import get_near_account

# Service imports
from src.services.llm_service import LLMService, ModelType
from src.services.schedule_service import ScheduleService
from src.services.monitoring_service import LimitOrderMonitoringService

# Manager imports
from src.managers.tool_state_manager import ToolStateManager, ToolOperationState
from src.db.mongo_manager import MongoManager
from src.managers.schedule_manager import ScheduleManager
from src.managers.approval_manager import ApprovalManager, ApprovalAction

# Utility imports
from src.utils.trigger_detector import TriggerDetector
from src.utils.json_parser import parse_strict_json, extract_json

# Prompt imports
from src.prompts.tool_prompts import ToolPrompts

# DB enums
from src.db.enums import (
    AgentState, 
    ToolOperationState, 
    OperationStatus, 
    ContentType, 
    ToolType,
    ApprovalState,
    ScheduleState
)

load_dotenv()
logger = logging.getLogger(__name__)

class Orchestrator:
    """Core tool orchestrator"""
    
    def __init__(self, deps: Optional[AgentDependencies] = None):
        """Initialize orchestrator with tools and dependencies"""
        self.deps = deps or AgentDependencies(session_id="default")
        self.tools = {}
        self.schedule_service = None  # Initialize as None
        self.monitoring_service = None  # Add monitoring service reference
        
        # Initialize core services first
        self.llm_service = LLMService({
            "model_type": ModelType.GROQ_LLAMA_3_3_70B
        })
        self.trigger_detector = TriggerDetector()
        
        # Get database instance
        db = MongoManager.get_db()
        if not db:
            logger.warning("MongoDB not initialized, attempting to initialize...")
            asyncio.create_task(MongoManager.initialize(os.getenv('MONGO_URI')))
            db = MongoManager.get_db()
            if not db:
                raise ValueError("Failed to initialize MongoDB")
            
        # Initialize managers in correct order
        self.tool_state_manager = ToolStateManager(db=db)
        
        # Initialize schedule manager before approval manager
        self.schedule_manager = ScheduleManager(
            tool_state_manager=self.tool_state_manager,
            db=db,
            tool_registry={}  # Will be populated during tool registration
        )
        
        # Initialize approval manager with schedule_manager
        self.approval_manager = ApprovalManager(
            tool_state_manager=self.tool_state_manager,
            schedule_manager=self.schedule_manager,
            db=db,
            llm_service=self.llm_service
        )
        
        # Initialize CoinGecko client for price monitoring
        self.coingecko_client = CoinGeckoClient(api_key=os.getenv('COINGECKO_API_KEY'))
        
        # Initialize NEAR account
        self.near_account = get_near_account()
        if not self.near_account:
            logger.warning("NEAR account could not be initialized - limit orders will not work")
        else:
            logger.info("NEAR account initialized successfully")
        
        # Register tools
        self._register_twitter_tool()
        self._register_intents_tool()
        
        # Log registered tools for debugging
        logger.info(f"Registered tools: {list(self.tools.keys())}")

    def _register_twitter_tool(self): # register all tools?
        """Register only TwitterTool for testing"""
        try:
            # Get registry requirements from TwitterTool
            registry = TwitterTool.registry

            # Initialize tool with just deps
            tool = TwitterTool(deps=AgentDependencies(session_id="test_session"))
            
            # Inject required services based on registry
            tool.inject_dependencies(
                tool_state_manager=self.tool_state_manager,
                llm_service=self.llm_service,
                approval_manager=self.approval_manager,
                schedule_manager=self.schedule_manager
            )

            # Register tool
            self.tools[registry.tool_type.value] = tool
            logger.info(f"Successfully registered TwitterTool")
            
        except Exception as e:
            logger.error(f"Failed to register TwitterTool: {e}")
            raise

    def register_tool(self, tool: BaseTool):
        """Enhanced tool registration"""
        self.tools[tool.name] = tool
        
        # Use tool's registry directly for schedule manager registration
        if tool.registry.requires_scheduling:
            self.schedule_manager.tool_registry[tool.registry.content_type.value] = tool
            logger.info(f"Registered schedulable tool: {tool.name} for content type: {tool.registry.content_type.value}")

    def set_schedule_service(self, schedule_service):
        """Set the schedule service instance"""
        self.schedule_service = schedule_service

    def set_monitoring_service(self, monitoring_service):
        """Set the monitoring service instance"""
        self.monitoring_service = monitoring_service
        
        # Inject monitoring service into schedule manager
        if self.schedule_manager:
            self.schedule_manager.inject_services(
                monitoring_service=monitoring_service
            )

    async def initialize(self):
        """Initialize async components"""
        # Initialize tools if any
        for tool in self.tools.values():
            if hasattr(tool, 'initialize'):
                await tool.initialize()
        
        # Start schedule service if it exists
        if self.schedule_service:
            await self.schedule_service.start()
        
        # Start monitoring service if it exists
        if self.monitoring_service:
            await self.monitoring_service.start()

    async def cleanup(self):
        """Cleanup async resources"""
        # Stop schedule service
        if self.schedule_service:
            await self.schedule_service.stop()
        
        # Stop monitoring service
        if self.monitoring_service:
            await self.monitoring_service.stop()
        
        # Cleanup all tools
        for tool in self.tools.values():
            if hasattr(tool, 'cleanup'):
                await tool.cleanup()

    async def process_command(self, command: str, deps: AgentDependencies) -> AgentResult:
        try:
            # Get current operation state
            operation = await self.tool_state_manager.get_operation_state(deps.session_id)
            
            # Resolve and execute appropriate tool
            tool = self.resolve_tool(operation, command)
            result = await tool.run(command)
            
            # Handle state transitions
            if result.get("status") in ["completed", "cancelled", "error"]:
                await self.tool_state_manager.end_operation(
                    session_id=deps.session_id,
                    success=result.get("status") == "completed"
                )
            
            return AgentResult(
                response=result.get("response"),
                data={
                    "state": operation.get("state"),
                    "status": result.get("status"),
                    "tool_type": tool.name,
                    "requires_input": result.get("requires_input", False)
                }
            )

        except Exception as e:
            logger.error(f"Error in orchestrator: {e}")
            return AgentResult(
                response="I encountered an error processing your request.",
                data={"status": "error", "error": str(e)}
            )

    def _is_exit_command(self, command: str) -> bool:
        """Check if command is a global exit command"""
        exit_keywords = ["exit", "quit", "stop", "cancel", "done"]
        return any(keyword in command.lower() for keyword in exit_keywords)

    def initialize_tool(self, tool_class: Type[BaseTool]) -> BaseTool:
        """Initialize a tool with its required dependencies"""
        registry = tool_class.get_registry()
        
        # Initialize required clients
        clients = {}
        if "twitter_client" in registry.required_clients:
            clients["twitter_client"] = TwitterAgentClient()
        
        # Initialize required managers
        managers = {}
        if "approval_manager" in registry.required_managers:
            managers["approval_manager"] = self.approval_manager
        if "schedule_manager" in registry.required_managers:
            managers["schedule_manager"] = self.schedule_manager
        if "tool_state_manager" in registry.required_managers:
            managers["tool_state_manager"] = self.tool_state_manager
        
        # Initialize tool with dependencies
        return tool_class(
            deps=self.deps,
            **clients,
            **managers
        )

    async def handle_tool_operation(self, message: str, session_id: str, tool_type: Optional[str] = None) -> Dict:
        """Handle tool operations based on current state"""
        try:
            logger.info(f"Handling tool operation for session: {session_id}")
            
            # Validate tool_type against enum
            if tool_type and tool_type not in [t.value for t in ToolType]:
                logger.warning(f"Invalid tool_type: {tool_type}")
                return {
                    "response": "I encountered an error processing your request.",
                    "error": f"Invalid tool type: {tool_type}",
                    "status": "error"
                }
            
            # Get current operation
            operation = await self.tool_state_manager.get_operation(session_id)
            logger.info(f"Retrieved operation for session {session_id}: {operation['_id'] if operation else None}")
            
            # If operation exists but is in terminal state, clear it to force new operation
            if operation and operation.get('state') in [
                ToolOperationState.COMPLETED.value,
                ToolOperationState.ERROR.value,
                ToolOperationState.CANCELLED.value
            ]:
                logger.info(f"Previous operation {operation['_id']} was in terminal state {operation.get('state')}, starting new operation")
                operation = None  # Clear the operation to force creation of a new one
            
            # Keep the existing logic for handling ongoing or creating new operation
            if operation:
                return await self._handle_ongoing_operation(operation, message)

            # Rest of the existing code for creating new operation...
            operation = await self.tool_state_manager.start_operation(
                session_id=session_id,
                tool_type=tool_type,
                initial_data={
                    "command": message,
                    "tool_type": tool_type
                }
            )
            logger.info(f"Created new operation: {operation['_id']}")
            
            # Get the appropriate tool
            tool = self.tools.get(tool_type)
            if not tool:
                logger.error(f"Tool not found for type: {tool_type}")
                return {
                    "response": "I encountered an error processing your request.",
                    "error": f"Tool not found: {tool_type}",
                    "status": "error"
                }
            
            # Update tool's session ID
            tool.deps.session_id = session_id
            
            try:
                # First get command analysis
                command_analysis = await tool._analyze_command(message)
                
                # Then generate content using analysis results
                generation_result = await tool._generate_content(
                    topic=command_analysis["topic"],
                    count=command_analysis["item_count"],
                    schedule_id=command_analysis.get("schedule_id"),
                    tool_operation_id=str(operation['_id'])
                )
                
                # Update operation with tool registry info and generated content
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    tool_operation_id=str(operation['_id']),
                    input_data={
                        "command": message,
                        "tool_registry": {
                            "requires_approval": tool.registry.requires_approval,
                            "requires_scheduling": tool.registry.requires_scheduling,
                            "content_type": tool.registry.content_type.value,
                            "tool_type": tool.registry.tool_type.value
                        },
                        **command_analysis  # Include analysis results
                    },
                    content_updates={
                        "items": generation_result["items"]  # Store generated items
                    }
                )
                
                # Now determine next state based on requirements
                if tool.registry.requires_approval:
                    # Move to approval flow with the generated items
                    logger.info(f"Moving operation {operation['_id']} to APPROVING state")
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        tool_operation_id=str(operation['_id']),
                        state=ToolOperationState.APPROVING.value
                    )
                    return await self._handle_approval_flow(
                        operation=operation,
                        message=message,
                        items=generation_result["items"]  # Pass the generated items
                    )
                else:
                    # Move to execution
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        tool_operation_id=str(operation['_id']),
                        state=ToolOperationState.EXECUTING.value
                    )
                    
                    if tool.registry.requires_scheduling:
                        return await self._handle_scheduled_operation(operation, generation_result["items"])
                    else:
                        # Execute immediately
                        result = await tool.run(message)
                        await self.tool_state_manager.end_operation(
                            session_id=session_id,
                            tool_operation_id=str(operation['_id']),
                            success=True,
                            api_response=result
                        )
                        return result

            except Exception as e:
                logger.error(f"Error processing tool operation: {e}")
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    tool_operation_id=str(operation['_id']),
                    state=ToolOperationState.ERROR.value
                )
                raise

            # When operation reaches a terminal state
            if operation and operation.get("state") in [ToolOperationState.COMPLETED.value, 
                                                      ToolOperationState.CANCELLED.value, 
                                                      ToolOperationState.ERROR.value]:
                # Get operation details
                operation_data = await self.tool_state_manager.get_operation_by_id(str(operation['_id']))
                
                # Generate operation summary
                summary = await self._generate_operation_summary(
                    operation_data=operation_data,
                    message=message,
                    tool_type=tool_type
                )

                return {
                    "status": operation.get("state").lower(),
                    "state": operation.get("state"),
                    "tool_type": tool_type,
                    "requires_chat_response": True,
                    "operation_summary": {
                        "summary": summary,
                        "operation_id": str(operation['_id']),
                        "execution_type": "background" if operation_data.get('metadata', {}).get('schedule_state') else "immediate",
                        "raw_data": operation_data
                    }
                }
            
            # For ongoing operations
            return {
                "response": result,
                "status": "ongoing",
                "state": operation.get("state") if operation else "unknown",
                "tool_type": tool_type
            }

        except Exception as e:
            logger.error(f"Error in handle_tool_operation: {e}")
            
            # Try to end the operation with error status
            try:
                operation = await self.tool_state_manager.get_operation(session_id)
                if operation:
                    await self.tool_state_manager.end_operation(
                        session_id=session_id,
                        success=False,
                        api_response={"error": str(e)},
                        step="error"
                    )
                    logger.info(f"Operation {operation['_id']} marked as error")
            except Exception as end_error:
                logger.error(f"Failed to end operation with error: {end_error}")
            
            # Ensure errors also trigger state transition
            return {
                "error": str(e),
                "response": f"I encountered an error: {str(e)}",
                "status": "exit",
                "state": "error"
            }

    async def _handle_scheduled_operation(self, operation: Dict, approved_items: List[Dict]) -> Dict:
        try:
            # Get schedule info from operation
            schedule_info = operation.get('input_data', {}).get('command_info', {})
            schedule_id = operation.get('metadata', {}).get('schedule_id')
            
            if not schedule_id:
                raise ValueError("No schedule ID found for operation")
            
            # Activate schedule with approved items
            success = await self.schedule_manager.activate_schedule(
                tool_operation_id=str(operation['_id']),
                schedule_id=schedule_id
            )
            
            if success:
                return {
                    "success": True,
                    "response": "Schedule activated successfully",
                    "schedule_id": schedule_id
                }
            
            return {
                "success": False,
                "error": "Failed to activate schedule"
            }
            
        except Exception as e:
            logger.error(f"Error handling scheduled operation: {e}")
            return {"success": False, "error": str(e)}

    async def _handle_approval_flow(self, operation: Dict, message: str, items: List[Dict]) -> Dict:
        """Handle operations requiring approval"""
        try:
            # 1. Start approval flow
            result = await self.approval_manager.start_approval_flow(
                session_id=operation['session_id'],
                tool_operation_id=str(operation['_id']),
                items=items,
                message=message
            )

            # 2. After approval, check scheduling needs
            if result.get('approval_state') == ApprovalState.APPROVAL_FINISHED.value:
                requires_scheduling = operation.get('metadata', {}).get('requires_scheduling', False)
                
                if requires_scheduling:
                    # Handle scheduling for approved items
                    schedule_result = await self._handle_scheduled_operation(operation, items)
                    return schedule_result
                else:
                    # Execute approved items immediately
                    execution_result = await tool.execute_approved_items(operation)
                    await self.tool_state_manager.end_operation(
                        session_id=operation['session_id'],
                        tool_operation_id=str(operation['_id']),
                        success=True,
                        api_response=execution_result
                    )
                    return execution_result

            # 3. Return approval flow result for other states
            return result

        except Exception as e:
            logger.error(f"Error in approval flow: {e}")
            raise

    async def _handle_ongoing_operation(self, operation: Dict, message: str) -> Dict:
        try:
            current_state = operation.get('state')
            tool_type = operation.get('tool_type')
            logger.info(f"Handling ongoing operation {operation['_id']} in state {current_state}")

            original_count = operation.get('input_data', {}).get('command_info', {}).get('item_count', 0)
            logger.info(f"Original requested count: {original_count}")

            # Get the tool instance
            tool = self.tools.get(tool_type)
            if not tool:
                logger.error(f"Tool not found for type: {tool_type}")
                return {
                    "response": "I encountered an error processing your request.",
                    "error": f"Tool not found: {tool_type}",
                    "status": "error"
                }

            if current_state == ToolOperationState.APPROVING.value:
                # Get ONLY active pending items from current turn
                current_items = await self.tool_state_manager.get_operation_items(
                    tool_operation_id=str(operation['_id']),
                    state=ToolOperationState.APPROVING.value,
                    status=OperationStatus.PENDING.value,
                    additional_query={
                        "metadata.rejected_at": {"$exists": False},
                        "content": {"$exists": True, "$ne": ""},
                        "_id": {"$in": [ObjectId(id) for id in operation.get('metadata', {}).get('active_items', [])]}
                    }
                )

                logger.info(f"Processing approval response for {len(current_items)} items in operation {operation['_id']}")

                # Handle approval through ApprovalManager
                approval_result = await self.approval_manager.process_approval_response(
                    message=message,
                    session_id=operation['session_id'],
                    content_type=operation.get('metadata', {}).get('content_type'),
                    tool_operation_id=str(operation['_id']),
                    handlers={
                        "full_approval": lambda tool_operation_id, session_id, items, analysis, **kwargs:
                            self.approval_manager._handle_full_approval(
                                tool_operation_id=tool_operation_id,
                                session_id=session_id,
                                items=current_items,  # Pass only current turn's items
                                analysis=analysis
                            ),
                        "partial_approval": lambda tool_operation_id, session_id, analysis, **kwargs:
                            self.approval_manager.handle_partial_approval(
                                session_id=session_id,
                                tool_operation_id=tool_operation_id,
                                analysis=analysis
                            ),
                        "regenerate_all": lambda tool_operation_id, session_id, analysis, **kwargs:
                            self.approval_manager.handle_regenerate_all(
                                session_id=session_id,
                                tool_operation_id=tool_operation_id,
                                analysis=analysis
                            ),
                        "exit": lambda tool_operation_id, session_id, **kwargs:
                            self.approval_manager.handle_exit(
                                session_id=session_id,
                                tool_operation_id=tool_operation_id,
                                success=False,
                                tool_type=tool_type
                            )
                    }
                )

                # Check if we need to generate new content
                if approval_result.get("status") == "regeneration_needed":
                    # Keep existing regeneration logic
                    collecting_items = await self.tool_state_manager.get_operation_items(
                        tool_operation_id=str(operation['_id']),
                        state=ToolOperationState.COLLECTING.value
                    )
                    
                    if collecting_items:
                        # Generate new content for existing items
                        generation_result = await tool._generate_content(
                            topic=operation.get('input_data', {}).get('topic'),
                            count=len(collecting_items),  # Use existing items count
                            revision_instructions=approval_result.get("data", {}).get("analysis", {}).get("revision_instructions"),
                            schedule_id=operation.get('metadata', {}).get('schedule_id'),
                            tool_operation_id=str(operation['_id'])
                        )
                        
                        # Update existing items with new content
                        for item, new_content in zip(collecting_items, generation_result["items"]):
                            await self.tool_state_manager.update_tool_item(
                                tool_operation_id=str(operation['_id']),
                                item_id=str(item['_id']),
                                content=new_content,
                                state=ToolOperationState.APPROVING.value
                            )

                        # Start new approval flow with updated items
                        return await self.approval_manager.start_approval_flow(
                            session_id=operation['session_id'],
                            tool_operation_id=str(operation['_id']),
                            items=collecting_items,
                            message=message
                        )
                    else:
                        logger.error("No items found in COLLECTING state for regeneration")
                        return {
                            "status": "error",
                            "response": "No items found in COLLECTING state for regeneration",
                            "requires_tts": True
                        }

                # After approval (not regeneration), check if we've met our count
                elif approval_result.get("status") == OperationStatus.APPROVED.value:
                    # Get total approved items
                    approved_items = await self.tool_state_manager.get_operation_items(
                        tool_operation_id=str(operation['_id']),
                        state=ToolOperationState.EXECUTING.value,
                        status=OperationStatus.APPROVED.value
                    )
                    
                    logger.info(f"Found {len(approved_items)} approved items out of {original_count} required")

                    # If we have all required items approved
                    if len(approved_items) >= original_count:
                        logger.info("Required item count reached, proceeding with completion flow")
                        
                        # Check if scheduling is required
                        if tool.registry.requires_scheduling:
                            schedule_result = await self._handle_scheduled_operation(
                                operation=operation,
                                approved_items=approved_items[:original_count]
                            )
                            if schedule_result.get("success"):
                                return {
                                    "status": "completed",
                                    "state": ToolOperationState.COMPLETED.value,
                                    "message": "Items approved and scheduled successfully",
                                    "requires_chat_response": True
                                }
                        else:
                            # For non-scheduled operations, complete immediately
                            await self.tool_state_manager.end_operation(
                                session_id=operation['session_id'],
                                tool_operation_id=str(operation['_id']),
                                success=True,
                                api_response={"message": "All items processed successfully"}
                            )
                            
                            return {
                                "status": "completed",
                                "state": ToolOperationState.COMPLETED.value,
                                "response": f"All {original_count} items have been processed.",
                                "requires_tts": True
                            }

                return approval_result

            elif current_state == ToolOperationState.EXECUTING.value:
                # Handle execution state - check schedule status, etc.
                logger.info(f"Operation {operation['_id']} is in EXECUTING state")
                
                # Check if this is a scheduled operation that needs activation
                if tool.registry.requires_scheduling:
                    schedule_id = (
                        operation.get('metadata', {}).get('schedule_id') or 
                        operation.get('output_data', {}).get('schedule_id') or
                        operation.get('input_data', {}).get('schedule_id')
                    )
                    
                    if schedule_id:
                        # Check schedule status
                        schedule = await self.db.get_scheduled_operation(schedule_id)
                        
                        if schedule and schedule.get('state') == ScheduleState.PENDING.value:
                            logger.info(f"Found pending schedule {schedule_id} that needs activation")
                            
                            # Activate the schedule
                            activation_result = await self.schedule_manager.activate_schedule(
                                tool_operation_id=str(operation['_id']),
                                schedule_id=schedule_id
                            )
                            
                            if activation_result:
                                logger.info(f"Schedule {schedule_id} activated successfully")
                                
                                # Use end_operation to properly mark the operation as complete
                                updated_operation = await self.tool_state_manager.end_operation(
                                    session_id=operation['session_id'],
                                    success=True,
                                    api_response={"message": "Schedule activated successfully"},
                                    step="completed"
                                )
                                
                                logger.info(f"Operation {operation['_id']} marked as completed")
                                
                                # Get topic and count for user-friendly response
                                topic = operation.get('input_data', {}).get('topic', 'your content')
                                count = len(await self.tool_state_manager.get_operation_items(
                                    tool_operation_id=str(operation['_id']),
                                    state=ToolOperationState.EXECUTING.value
                                ))
                                
                                # Return with "completed" status to trigger state transition
                                return {
                                    "status": "completed",  # Signal completion for state transition
                                    "response": f"Great! I've scheduled {count} items about {topic}. They will be posted according to your schedule.",
                                    "requires_tts": True,
                                    "state": ToolOperationState.COMPLETED.value  # Include the state
                                }
                        
                        elif schedule:
                            return {
                                "status": "ongoing",  # Still in progress
                                "response": f"Your content is scheduled and will be posted according to the schedule. Current status: {schedule.get('state')}",
                                "requires_tts": True
                            }
                
                return {
                    "status": "ongoing",
                    "response": "Your content is being processed.",
                    "requires_tts": True
                }

            # Handle terminal states
            elif current_state in [ToolOperationState.COMPLETED.value, 
                                 ToolOperationState.ERROR.value,
                                 ToolOperationState.CANCELLED.value]:
                logger.info(f"Operation {operation['_id']} in terminal state: {current_state}")
                
                # For terminal states, return the appropriate status to trigger state transition
                status_mapping = {
                    ToolOperationState.COMPLETED.value: "completed",
                    ToolOperationState.ERROR.value: "error",
                    ToolOperationState.CANCELLED.value: "cancelled"
                }
                
                return {
                    "response": "This operation has already been completed or cancelled.",
                    "state": current_state,
                    "status": status_mapping.get(current_state, "exit")  # Map to appropriate status for state transition
                }

            raise ValueError(f"Unexpected state/status combination: {current_state}/{operation.get('status')}")

        except Exception as e:
            logger.error(f"Error in _handle_ongoing_operation: {e}")
            # Ensure errors also trigger state transition
            return {
                "error": str(e),
                "response": f"I encountered an error: {str(e)}",
                "status": "exit",  # Signal exit on error
                "state": "error"
            }

    def _register_intents_tool(self):
        """Register IntentsTool for limit order operations"""
        try:
            # Import IntentsTool here to avoid circular imports
            from src.tools.intents_operation import IntentsTool
            
            # Get registry requirements from IntentsTool
            registry = IntentsTool.registry

            # Initialize tool with deps
            tool = IntentsTool(deps=self.deps)
            
            # Inject required services - importantly, pass the NEAR account
            tool.inject_dependencies(
                tool_state_manager=self.tool_state_manager,
                llm_service=self.llm_service,
                approval_manager=self.approval_manager,
                schedule_manager=self.schedule_manager,
                coingecko_client=self.coingecko_client,
                near_account=self.near_account  # This is the critical injection
            )

            # Register tool with the exact key that will be looked up
            self.tools[registry.tool_type.value] = tool
            
            # Also register in schedule_manager's tool_registry for scheduled operations
            self.schedule_manager.tool_registry[registry.content_type.value] = tool
            
            logger.info(f"Successfully registered IntentsTool with key: {registry.tool_type.value}")
            
        except Exception as e:
            logger.error(f"Failed to register IntentsTool: {e}")
            logger.exception("IntentsTool registration failed with exception:")  # Log full traceback

    async def _generate_operation_summary(self, operation_data: Dict, message: str, tool_type: str) -> str:
        """Generate a consistent summary for completed operations"""
        summary_prompt = f"""Summarize this tool operation result for the user:
Operation Type: {tool_type}
Status: {operation_data.get('state')}
Input Command: {message}

Operation Details:
- Initial Request: {operation_data.get('input_data', {}).get('command')}
- Execution State: {operation_data.get('state')}
- Schedule Status: {operation_data.get('metadata', {}).get('schedule_state', 'immediate')}

Generated Content:
{json.dumps(operation_data.get('content_updates', {}), indent=2)}

Final Result:
{json.dumps(operation_data.get('output_data', {}), indent=2)}

Format the summary to:
1. Confirm what was set up/scheduled
2. Explain what will happen next
3. Provide any relevant immediate results
4. Note any background processes that will continue"""

        return await self.llm_service.get_response(
            prompt=[{"role": "user", "content": summary_prompt}],
            model_type=ModelType.CLAUDE_3_5_SONNET
        )

    async def _handle_approved_operation(self, operation: Dict, tool: BaseTool, approval_result: Dict) -> Dict:
        try:
            # Get all approved items
            approved_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=str(operation['_id']),
                state=ToolOperationState.EXECUTING.value
            )
            
            if tool.registry.requires_scheduling:
                # Handle scheduling
                schedule_result = await self._handle_scheduled_operation(operation, approved_items)
                if schedule_result.get("success"):
                    # End operation with success
                    await self.tool_state_manager.end_operation(
                        session_id=operation['session_id'],
                        tool_operation_id=str(operation['_id']),
                        success=True,
                        step="scheduled"
                    )
                    return {
                        "status": "completed",
                        "state": ToolOperationState.COMPLETED.value,
                        "response": "Your content has been scheduled successfully!",
                        "requires_chat_response": True
                    }
            
            # End operation
            await self.tool_state_manager.end_operation(
                session_id=operation['session_id'],
                tool_operation_id=str(operation['_id']),
                success=True,
                step="completed"
            )
            
            return {
                "status": "completed",
                "state": ToolOperationState.COMPLETED.value,
                "response": "Operation completed successfully!",
                "requires_chat_response": True
            }
        except Exception as e:
            logger.error(f"Error in _handle_approved_operation: {e}")
            raise