from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Optional, Any, Type
import logging
import asyncio
import os
from dotenv import load_dotenv
from datetime import datetime, UTC, timedelta
import json

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

# Service imports
from src.services.llm_service import LLMService, ModelType
from src.services.schedule_service import ScheduleService

# Manager imports
from src.managers.tool_state_manager import ToolStateManager, ToolOperationState
from src.db.mongo_manager import MongoManager
from src.managers.schedule_manager import ScheduleManager
from src.managers.approval_manager import ApprovalManager

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
    ApprovalState
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
        
        # Register TwitterTool last after all managers are initialized
        self._register_twitter_tool()

    def _register_twitter_tool(self):
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

    async def initialize(self):
        """Initialize async components"""
        # Initialize tools if any
        for tool in self.tools.values():
            if hasattr(tool, 'initialize'):
                await tool.initialize()
        
        # Start schedule service if it exists
        if self.schedule_service:
            await self.schedule_service.start()

    async def cleanup(self):
        """Cleanup async resources"""
        # Stop schedule service
        await self.schedule_service.stop()
        
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
            
            # Get or create operation
            operation = await self.tool_state_manager.get_operation(session_id)
            logger.info(f"Retrieved operation for session {session_id}: {operation['_id'] if operation else None}")
            
            # If operation exists, check its state to determine flow
            if operation:
                current_state = operation.get('state')
                logger.info(f"Operation {operation['_id']} in state: {current_state}")
                
                if current_state == ToolOperationState.APPROVING.value:
                    # Handle approval response through approval manager
                    logger.info(f"Processing approval response for operation {operation['_id']}")
                    return await self._handle_ongoing_operation(operation, message)
                
                # ... handle other states ...

            # If no operation, start new one
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
                        return await self._handle_scheduled_operation(operation, message)
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

        except Exception as e:
            logger.error(f"Error in handle_tool_operation: {e}")
            raise

    async def _handle_scheduled_operation(self, operation: Dict, message: str) -> Dict:
        """Initialize and activate a schedule for operation"""
        try:
            # 1. Initialize schedule if not exists
            schedule_id = operation.get('metadata', {}).get('schedule_id')
            if not schedule_id:
                schedule_id = await self.schedule_manager.initialize_schedule(
                    tool_operation_id=str(operation['_id']),
                    schedule_info=operation.get('metadata', {}).get('schedule_params', {}),
                    content_type=operation.get('content_type'),
                    session_id=operation['session_id']
                )
                if not schedule_id:
                    raise ValueError("Failed to initialize schedule")

            # 2. Activate schedule (moves items to SCHEDULED status)
            success = await self.schedule_manager.activate_schedule(
                tool_operation_id=str(operation['_id']),
                schedule_id=schedule_id
            )

            if success:
                # 3. End operation (ToolOperation is COMPLETED, items remain EXECUTING/SCHEDULED)
                await self.tool_state_manager.end_operation(
                    session_id=operation['session_id'],
                    tool_operation_id=str(operation['_id']),
                    success=True,
                    api_response={"message": "Operation scheduled successfully"},
                    metadata={
                        "schedule_id": schedule_id,
                        "requires_scheduling": True
                    }
                )
                return {
                    "success": True, 
                    "response": "Operation scheduled successfully",
                    "state": ToolOperationState.COMPLETED.value,
                    "status": OperationStatus.SCHEDULED.value
                }

            return {"success": False, "response": "Failed to schedule operation"}

        except Exception as e:
            logger.error(f"Error in _handle_scheduled_operation: {e}")
            return {"success": False, "response": str(e)}

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
                    schedule_result = await self._handle_scheduled_operation(operation, message)
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
        """Handle ongoing operations based on current state"""
        try:
            current_state = operation.get('state')
            tool_type = operation.get('tool_type')
            current_status = operation.get('status', OperationStatus.PENDING.value)
            
            logger.info(f"Handling ongoing operation {operation['_id']} in state {current_state}")
            
            # Get the tool instance
            tool = self.tools.get(tool_type)
            if not tool:
                logger.error(f"Tool not found for type: {tool_type}")
                return {
                    "response": "I encountered an error processing your request.",
                    "error": f"Tool not found: {tool_type}",
                    "status": "error"
                }

            if current_state == ToolOperationState.COLLECTING.value:
                logger.info(f"Processing collection state for operation {operation['_id']}")
                return await tool.run(message)
            
            elif current_state == ToolOperationState.APPROVING.value:
                # Get current items for approval using tool_state_manager
                current_items = await self.tool_state_manager.get_operation_items(
                    tool_operation_id=str(operation['_id']),
                    state=ToolOperationState.APPROVING.value
                )

                logger.info(f"Processing approval response for {len(current_items)} items in operation {operation['_id']}")

                if not current_items:
                    logger.warning(f"No items found for approval in operation {operation['_id']}")
                    return {
                        "response": "No items found for approval",
                        "status": "error"
                    }

                # Handle approval through ApprovalManager
                result = await self.approval_manager.process_approval_response(
                    message=message,
                    session_id=operation['session_id'],
                    content_type=operation.get('metadata', {}).get('content_type'),
                    tool_operation_id=str(operation['_id']),
                    handlers={
                        ApprovalAction.PARTIAL_APPROVAL.value: tool._regenerate_rejected_items,
                        ApprovalAction.REGENERATE_ALL.value: tool._regenerate_rejected_items,
                        ApprovalAction.FULL_APPROVAL.value: lambda tool_operation_id, session_id, analysis, **kwargs:
                            self.approval_manager._handle_full_approval(
                                tool_operation_id=tool_operation_id,
                                session_id=session_id,
                                items=current_items,
                                analysis=analysis
                            ),
                        ApprovalAction.EXIT.value: lambda **kwargs: self.approval_manager.handle_exit(
                            session_id=operation['session_id'],
                            tool_operation_id=str(operation['_id']),
                            success=False,
                            tool_type=tool_type
                        )
                    }
                )

                # Handle regeneration results
                if result.get("items"):
                    logger.info(f"Starting new approval flow with {len(result['items'])} regenerated items")
                    return await self.approval_manager.start_approval_flow(
                        session_id=operation['session_id'],
                        tool_operation_id=str(operation['_id']),
                        items=result["items"]
                    )

                return result

            # Handle terminal states
            elif current_state in [ToolOperationState.COMPLETED.value, 
                                 ToolOperationState.ERROR.value,
                                 ToolOperationState.CANCELLED.value]:
                logger.info(f"Operation {operation['_id']} in terminal state: {current_state}")
                return {
                    "response": "This operation has already been completed or cancelled.",
                    "state": current_state,
                    "status": current_status
                }

            raise ValueError(f"Unexpected state/status combination: {current_state}/{current_status}")

        except Exception as e:
            logger.error(f"Error in _handle_ongoing_operation: {e}")
            raise