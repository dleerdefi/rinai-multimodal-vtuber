from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Optional, Any
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
    CalendarToolParameters
)

# Tool imports
from src.tools.crypto_data import CryptoTool
from src.tools.post_tweets import TwitterTool
from src.tools.perplexity_search import PerplexityTool
from src.tools.time_tools import TimeTool
from src.tools.weather_tools import WeatherTool
from src.tools.calendar_tool import CalendarTool

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

# Utility imports
from src.utils.trigger_detector import TriggerDetector
from src.utils.json_parser import parse_strict_json, extract_json

# Prompt imports
from src.prompts.tool_prompts import ToolPrompts

load_dotenv()
logger = logging.getLogger(__name__)

class Orchestrator:
    """Core tool orchestrator"""
    
    def __init__(self, deps: Optional[AgentDependencies] = None):
        """Initialize orchestrator with tools and dependencies"""
        self.deps = deps
        self.tools = {}
        
        # Get database instance
        db = MongoManager.get_db()
        if not db:
            raise ValueError("Database not initialized")
            
        # Properly initialize ToolStateManager with db
        self.tool_state_manager = ToolStateManager(db=db)
        self.trigger_detector = TriggerDetector()
        
        # Initialize LLM service
        self.llm_service = LLMService({
            "model_type": ModelType.GROQ_LLAMA_3_3_70B
        })
        
        # Initialize services
        self.schedule_service = ScheduleService(
            mongo_uri=os.getenv("MONGO_URI"),
            orchestrator=self
        )
        
        # Initialize tools with their dependencies
        self.crypto_tool = CryptoTool(self._init_coingecko())
        self.perplexity_tool = PerplexityTool(self._init_perplexity())
        self.twitter_tool = TwitterTool(
            tool_state_manager=self.tool_state_manager,
            llm_service=self.llm_service,
            deps=self.deps
        )
        
        calendar_client = self._init_calendar()
        self.calendar_tool = CalendarTool(calendar_client=calendar_client)
        
        self.time_tool = TimeTool()
        self.weather_tool = WeatherTool()
        
        # Register tools properly using BaseTool interface
        self.register_tool(self.crypto_tool)
        self.register_tool(self.twitter_tool)
        self.register_tool(self.perplexity_tool)
        self.register_tool(self.calendar_tool)
        self.register_tool(self.time_tool)
        self.register_tool(self.weather_tool)
        
    async def initialize(self):
        """Initialize async components"""
        # Start schedule service
        await self.schedule_service.start()
        
        # Initialize tools that support async initialization
        if self.crypto_tool:
            if hasattr(self.crypto_tool, 'initialize'):
                await self.crypto_tool.initialize()
            
        if self.perplexity_tool:
            if hasattr(self.perplexity_tool, 'initialize'):
                await self.perplexity_tool.initialize()
        
        if self.calendar_tool:
            if hasattr(self.calendar_tool, 'initialize'):
                await self.calendar_tool.initialize()
        
    async def cleanup(self):
        """Cleanup async resources"""
        # Stop schedule service
        await self.schedule_service.stop()
        
        # Cleanup specific tools
        if self.crypto_tool:
            if hasattr(self.crypto_tool, 'cleanup'):
                await self.crypto_tool.cleanup()
            
        if self.perplexity_tool:
            if hasattr(self.perplexity_tool, 'cleanup'):
                await self.perplexity_tool.cleanup()
                
        if self.calendar_tool:
            if hasattr(self.calendar_tool, 'cleanup'):
                await self.calendar_tool.cleanup()
        
        for tool in self.tools.values():
            if hasattr(tool, 'cleanup'):
                await tool.cleanup()
        
    async def process_command(self, command: str, deps: Optional[AgentDependencies] = None) -> AgentResult:
        """Process command while maintaining tool state"""
        try:
            # 1. Get current operation state
            operation = None
            if deps and deps.session_id:
                operation = await self.tool_state_manager.get_operation_state(deps.session_id)
                logger.info(f"[ORCHESTRATOR] Current operation: {operation}")

            # 2. Check for global exit commands first
            if self._is_exit_command(command) and operation:
                tool = self.tools.get(operation.get("tool_type"))
                if tool and hasattr(tool, "approval_manager"):
                    result = await tool.approval_manager.handle_exit(
                        session_id=deps.session_id,
                        success=False,
                        tool_type=tool.name
                    )
                    return AgentResult(
                        response=result.get("response"),
                        data={
                            "tool_type": tool.name,
                            "status": "cancelled",
                            "completion_type": "user_exit"
                        }
                    )

            # 3. Resolve tool (either from existing operation or new command)
            tool = None
            if operation:
                tool = self.tools.get(operation.get("tool_type"))
                if tool:
                    tool.deps = deps
                    tool.deps.context = {
                        "operation": operation,
                        "step": operation.get("step"),
                        "schedule_id": operation.get("output_data", {}).get("schedule_id")
                    }
            else:
                tool_type = self.trigger_detector.get_specific_tool_type(command)
                tool = self.tools.get(tool_type)

            if not tool:
                return AgentResult(
                    response="I'm not sure how to handle that request.",
                    data={"status": "error"}
                )

            # 4. Execute tool
            result = await tool.run(command)

            # 5. Process result based on status
            status = result.get("status")
            if status in ["completed", "cancelled", "error"]:
                # Tool operation is ending
                return AgentResult(
                    response=result.get("response"),
                    data={
                        "tool_type": tool.name,
                        "status": status,
                        "completion_type": result.get("data", {}).get("completion_type"),
                        "final_status": result.get("data", {}).get("final_status")
                    }
                )
            
            # 6. Return standardized response for ongoing operations
            return AgentResult(
                response=result.get("response"),
                data={
                    "tool_type": tool.name,
                    "operation_state": operation.get("state") if operation else None,
                    "operation_step": operation.get("step") if operation else None,
                    "status": status,
                    "requires_input": True
                }
            )

        except Exception as e:
            logger.error(f"Error in orchestrator: {e}")
            # Try to get tool type for proper exit handling
            tool_type = operation.get("tool_type") if operation else None
            if tool_type and deps and deps.session_id:
                tool = self.tools.get(tool_type)
                if tool and hasattr(tool, "approval_manager"):
                    result = await tool.approval_manager.handle_exit(
                        session_id=deps.session_id,
                        success=False,
                        tool_type=tool_type
                    )
                    return AgentResult(
                        response=result.get("response"),
                        data={
                            "tool_type": tool_type,
                            "status": "error",
                            "error": str(e)
                        }
                    )
            
            return AgentResult(
                response="I encountered an error processing your request.",
                data={"status": "error", "error": str(e)}
            )

    def register_tool(self, tool: BaseTool):
        """Register a tool with the orchestrator"""
        self.tools[tool.name] = tool

    def _init_coingecko(self) -> Optional[CoinGeckoClient]:
        """Initialize CoinGecko client if configured"""
        try:
            api_key = os.getenv("COINGECKO_API_KEY")
            if not api_key:
                logger.warning("CoinGecko API key not found in environment variables")
                return None
            
            return CoinGeckoClient(api_key)
        except Exception as e:
            logger.warning(f"Failed to initialize CoinGecko client: {e}")
            return None

    def _init_perplexity(self) -> Optional[PerplexityClient]:
        """Initialize Perplexity client if configured"""
        try:
            api_key = os.getenv("PERPLEXITY_API_KEY")
            if not api_key:
                logger.warning("Perplexity API key not found in environment variables")
                return None
            
            return PerplexityClient(api_key)
        except Exception as e:
            logger.warning(f"Failed to initialize Perplexity client: {e}")
            return None

    def _init_calendar(self) -> Optional[GoogleCalendarClient]:
        """Initialize Google Calendar client if configured"""
        try:
            calendar_client = GoogleCalendarClient()
            return calendar_client
        except Exception as e:
            logger.warning(f"Failed to initialize Google Calendar client: {e}")
            return None

    def _is_exit_command(self, command: str) -> bool:
        """Check if command is a global exit command"""
        exit_keywords = ["exit", "quit", "stop", "cancel", "done"]
        return any(keyword in command.lower() for keyword in exit_keywords)