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
from src.tools.post_tweets import TweetTool
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

# Manager imports
from src.managers.tool_state_manager import ToolStateManager
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
        # Store deps first
        self.deps = deps
        
        # Initialize LLM service
        self.llm_service = LLMService({
            "model_type": ModelType.GROQ_LLAMA_3_3_70B
        })
        
        # Initialize tool state manager first
        self.tool_state_manager = ToolStateManager(MongoManager.get_db())
        
        # Initialize tools with their dependencies
        self.crypto_tool = CryptoTool(self._init_coingecko())
        self.perplexity_tool = PerplexityTool(self._init_perplexity())
        
        # Initialize tweet tool with proper dependencies
        self.tweet_tool = TweetTool(
            tool_state_manager=self.tool_state_manager,
            llm_service=self.llm_service,
            deps=self.deps
        )
        
        # Initialize calendar tool with client
        calendar_client = self._init_calendar()
        self.calendar_tool = CalendarTool(calendar_client=calendar_client)
        
        # Add other tools
        self.time_tool = TimeTool()
        self.weather_tool = WeatherTool()
        
        # Store tools in a dictionary for easy access
        self.tools = {
            "twitter": self.tweet_tool,
            "crypto_data": self.crypto_tool,
            "perplexity_search": self.perplexity_tool,
            "time_tools": self.time_tool,
            "weather_tools": self.weather_tool,
            "calendar_tool": self.calendar_tool
        }
        
    async def initialize(self):
        """Initialize async components"""
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
        if self.crypto_tool:
            if hasattr(self.crypto_tool, 'cleanup'):
                await self.crypto_tool.cleanup()
            
        if self.perplexity_tool:
            if hasattr(self.perplexity_tool, 'cleanup'):
                await self.perplexity_tool.cleanup()
                
        if self.calendar_tool:
            if hasattr(self.calendar_tool, 'cleanup'):
                await self.calendar_tool.cleanup()
        
    async def process_command(self, command: str, deps: Optional[AgentDependencies] = None, tool_type: Optional[str] = None) -> AgentResult:
        """Process a command and execute required tools
        
        Args:
            command: The command to process
            deps: Optional dependencies including conversation_id and user_id
            tool_type: Optional specific tool type detected by agent
        """
        try:
            # Store deps if provided and update tweet tool deps
            if deps:
                self.deps = deps
                if self.tweet_tool:
                    self.tweet_tool.deps = deps  # Update tweet tool's deps
            
            trigger_detector = TriggerDetector()
            
            # First check if this is a direct tool request (crypto/perplexity)
            if not tool_type:
                tool_type = trigger_detector.get_specific_tool_type(command)
            
            if tool_type in ["crypto_data", "perplexity_search", "time_tools", "weather_tools", "calendar_tool"]:
                logger.info(f"Processing direct tool request: {tool_type}")
                analysis = await self._analyze_command(command)
                if analysis and analysis.tools_needed:
                    results = await self._execute_tools(analysis.tools_needed)
                    return AgentResult(
                        response=self._format_response(results),
                        data=results
                    )
            
            # For Twitter operations, check the operation type first
            operation_type = trigger_detector.get_tool_operation_type(command)
            
            # If we have an active operation state, prioritize approval flow
            if self.deps and self.deps.conversation_id:
                operation_state = await self.tool_state_manager.get_operation_state(self.deps.conversation_id)
                
                if operation_state and operation_state.get("state") == "collecting":
                    # Only process as approval if it's NOT a new tweet request
                    if operation_type != "schedule_tweets":
                        logger.info("Processing as approval response")
                        tweet_tool = self.tools.get("twitter")
                        if not tweet_tool:
                            return AgentResult(
                                response="Twitter tool not configured",
                                data={"status": "error"}
                            )
                        
                        # Delegate to tweet tool's approval response handler
                        approval_result = await tweet_tool._process_tweet_approval_response(
                            message=command,
                            session_id=self.deps.conversation_id
                        )
                        return AgentResult(
                            response=approval_result.get("response", ""),
                            data={
                                "status": approval_result.get("status"),
                                "requires_tts": approval_result.get("requires_tts", True),
                                "tweet_data": approval_result.get("data", {})
                            }
                        )
            
            # Handle new Twitter commands
            if operation_type == "schedule_tweets":
                logger.info("Processing new tweet scheduling request")
                analysis = await self._analyze_command(command)
                if analysis and analysis.tools_needed:
                    results = await self._execute_tools(analysis.tools_needed)
                    return AgentResult(
                        response=self._format_response(results),
                        data=results
                    )
            
            # If no tool matches
            return AgentResult(
                response="I'm not sure how to handle that command.",
                data={"status": "error"}
            )
            
        except Exception as e:
            logger.error(f"Error in process_command: {e}", exc_info=True)
            return AgentResult(
                response="I encountered an error processing your command.",
                data={"error": str(e)}
            )
            
    async def _analyze_command(self, command: str) -> Optional[CommandAnalysis]:
        """Analyze command to determine required tools"""
        try:
            # Check if this is a Twitter command using TriggerDetector
            trigger_detector = TriggerDetector()
            
            # Check for Twitter commands first (maintain existing Twitter flow)
            if trigger_detector.should_use_twitter(command):
                logger.info("Processing as Twitter command")
                return await self.tweet_tool._analyze_twitter_command(command)
            
            # Get tool type if not already determined
            tool_type = trigger_detector.get_specific_tool_type(command)
            logger.debug(f"Tool type detected: {tool_type}")
            
            if not tool_type:
                return CommandAnalysis(
                    tools_needed=[],
                    reasoning="No specific tool type detected"
                )

            # For perplexity search, we can directly create the command analysis
            if tool_type == "perplexity_search":
                logger.info("Creating direct perplexity search command")
                return CommandAnalysis(
                    tools_needed=[
                        ToolCommand(
                            tool_name="perplexity_search",
                            action="search",
                            parameters={
                                "query": command,
                                "max_tokens": 300
                            },
                            priority=1
                        )
                    ],
                    reasoning="Query requires current information from web search"
                )

            # For other tools, use LLM analysis
            messages = []
            if tool_type == "time_tools":
                formatted_prompt = ToolPrompts.TIME_TOOL.format(command=command)
                messages = [
                    {"role": "system", "content": formatted_prompt}
                ]
            elif tool_type == "weather_tools":
                formatted_prompt = ToolPrompts.WEATHER_TOOL.format(command=command)
                messages = [
                    {"role": "system", "content": formatted_prompt}
                ]
            elif tool_type == "crypto_data":
                formatted_prompt = ToolPrompts.CRYPTO_TOOL.format(command=command)
                messages = [
                    {"role": "system", "content": formatted_prompt}
                ]
            elif tool_type == "calendar_tool":
                formatted_prompt = ToolPrompts.CALENDAR_TOOL.format(command=command)
                messages = [
                    {"role": "system", "content": formatted_prompt}
                ]

            if not messages:
                logger.warning(f"No prompt found for tool type: {tool_type}")
                return CommandAnalysis(
                    tools_needed=[],
                    reasoning=f"No prompt available for {tool_type}"
                )

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 300
                }
            )
            
            logger.debug(f"LLM Analysis Response: {response}")

            # For crypto, parse directly to CommandAnalysis
            data = parse_strict_json(response, CommandAnalysis)
            if data:
                return data
                
            logger.debug("No tools needed - returning empty analysis")
            return CommandAnalysis(
                tools_needed=[],
                reasoning="No special tools required"
            )

        except Exception as e:
            logger.error(f"Error analyzing command: {e}", exc_info=True)
            return None
            
    async def _execute_tools(self, tools: List[ToolCommand]) -> Dict:
        """Execute tools in parallel based on priority"""
        try:
            # Group tools by priority
            priority_groups = {}
            for tool in tools:
                priority_groups.setdefault(tool.priority, []).append(tool)
            
            results = {}
            for priority in sorted(priority_groups.keys()):
                group = priority_groups[priority]
                tasks = []
                
                for tool in group:
                    logger.debug(f"Processing tool: {tool.tool_name}, action: {tool.action}")
                    
                    if tool.tool_name == "twitter":
                        if tool.action == "schedule_tweets":
                            # Get tool instance from tools dict
                            tweet_tool = self.tools.get("twitter")
                            if not tweet_tool:
                                results[tool.tool_name] = {
                                    "status": "error",
                                    "error": "Twitter tool not configured",
                                    "timestamp": datetime.utcnow().isoformat()
                                }
                                continue

                            # Generate tweets
                            try:
                                tweets = await tweet_tool._generate_tweet_series(
                                    topic=tool.parameters.get("topic"),
                                    count=tool.parameters.get("tweet_count", 1),
                                    tone=tool.parameters.get("tone", "professional"),
                                    original_request=tool.parameters.get("original_request"),
                                    session_id=self.deps.conversation_id if self.deps else None
                                )
                                
                                # Handle approval flow
                                if self.deps and self.deps.conversation_id:
                                    approval_result = await tweet_tool._handle_tweet_approval_flow(
                                        tweets=tweets["tweets"],
                                        session_id=self.deps.conversation_id
                                    )
                                    results["twitter"] = approval_result
                                else:
                                    results["twitter"] = {
                                        "status": "pending_approval",
                                        "content": tweets["tweets"],
                                        "schedule": tool.parameters,
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                            except Exception as e:
                                logger.error(f"Error in tweet generation: {e}")
                                results["twitter"] = {
                                    "status": "error",
                                    "error": str(e),
                                    "timestamp": datetime.utcnow().isoformat()
                                }
                            continue  # Skip adding to tasks

                    # For other tools, use the tool instances
                    tool_instance = self.tools.get(tool.tool_name)
                    if tool_instance:
                        if tool.tool_name == "calendar_tool":
                            if tool.action == "get_schedule":
                                tasks.append(tool_instance.get_schedule(
                                    max_events=tool.parameters.get("max_events", 5)
                                ))
                        elif tool.tool_name == "perplexity_search":
                            tasks.append(tool_instance.search(
                                query=tool.parameters.get("query", ""),
                                max_tokens=tool.parameters.get("max_tokens", 300)
                            ))
                        elif tool.tool_name in ["crypto_data", "crypto_price"]:
                            tasks.append(tool_instance._get_crypto_data(
                                symbol=tool.parameters.get("symbol", "").upper(),
                                include_details=tool.parameters.get("include_details", False)
                            ))
                        elif tool.tool_name == "time_tools":
                            if tool.action == "get_time":
                                tasks.append(tool_instance.get_current_time_in_zone(
                                    tool.parameters.get("timezone")
                            ))
                        elif tool.action == "convert_time":
                            tasks.append(tool_instance.convert_time_between_zones(
                                from_timezone=tool.parameters.get("source_timezone"),
                                date_time=tool.parameters.get("source_time"),
                                to_timezone=tool.parameters.get("timezone")
                            ))
                        elif tool.tool_name == "weather_tools":
                            tasks.append(tool_instance.get_weather_data(
                                location=tool.parameters.get("location"),
                                units=tool.parameters.get("units", "metric")
                            ))
                    else:
                        results[tool.tool_name] = {
                            "status": "error",
                            "error": f"{tool.tool_name} tool is not configured",
                            "timestamp": datetime.utcnow().isoformat()
                        }
                
                if tasks:
                    group_results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Process results and handle any exceptions
                    for tool, result in zip(group, group_results):
                        if isinstance(result, Exception):
                            logger.error(f"Tool execution failed: {tool.tool_name}", exc_info=result)
                            results[tool.tool_name] = {
                                "status": "error",
                                "error": str(result),
                                "timestamp": datetime.utcnow().isoformat()
                            }
                        else:
                            results[tool.tool_name] = result
            
            logger.debug(f"Tool execution results: {results}")
            return results
            
        except Exception as e:
            logger.error(f"Error executing tools: {e}", exc_info=True)
            raise
        
    def _format_response(self, results: Dict) -> str:
        """Format results into a coherent response"""
        try:
            logger.info(f"Formatting results: {results}")
            
            # Handle Twitter responses
            if isinstance(results, dict) and 'twitter' in results:
                twitter_result = results['twitter']
                status = twitter_result.get('status')
                
                if status == 'pending_approval':
                    tweets = twitter_result.get('content', [])
                    schedule = twitter_result.get('schedule', {})
                    
                    response_parts = [
                        f"I've generated {len(tweets)} tweet(s) about {schedule.get('topic', 'the requested topic')}.",
                        "Here they are for your review:"
                    ]
                    
                    for i, tweet in enumerate(tweets, 1):
                        response_parts.append(f"\nTweet {i}:\n{tweet['content']}")
                    
                    response_parts.append("\nWould you like to approve these tweets for scheduling?")
                    return "\n".join(response_parts)
                
                elif status == 'awaiting_approval':
                    # Handle response from _handle_tweet_approval_flow
                    return twitter_result.get('response', "Please review the generated tweets.")
                
                elif status == 'error':
                    return twitter_result.get('response', "There was an error processing your request.")
            
            # If results is already a dict with requires_tts
            if isinstance(results, dict) and results.get("requires_tts"):
                logger.info("Found direct TTS response")
                return results["response"]
            
            response = []
            
            # Handle dictionary of tool results
            for tool_name, result in results.items():
                if isinstance(result, dict):
                    # First check if it's a direct response with requires_tts
                    if result.get("requires_tts") and result.get("response"):
                        logger.info(f"Found TTS response from {tool_name}")
                        return result["response"]
                    
                    # Then check if it's a success with data
                    elif result.get("status") == "success":
                        logger.info(f"Found success response from {tool_name}")
                        if "data" in result:
                            response.append(self._format_tool_data(tool_name, result["data"]))
                    
                    # Finally check for direct response
                    elif result.get("response"):
                        logger.info(f"Found direct response from {tool_name}")
                        response.append(result["response"])
                        
                else:
                    logger.warning(f"Unexpected result format from {tool_name}: {result}")
                    
            return "\n".join(response) if response else "I processed your request but didn't get a clear response. Could you try rephrasing?"
            
        except Exception as e:
            logger.error(f"Error formatting response: {e}", exc_info=True)
            return "I encountered an error processing the response. Could you try again?"
            
    def _format_tool_data(self, tool_name: str, data: Any) -> str:
        """Format specific tool data into readable response"""
        if tool_name.startswith("calendar"):
            tool = self.tools.get("calendar_tool")
            if tool:
                return tool._format_calendar_response(data)
            return f"Calendar data: {data}"
        elif tool_name.startswith("crypto"):
            tool = self.tools.get("crypto_data")
            if tool:
                return tool._format_crypto_response(data)
            return f"Crypto data: {data}"  # Fallback if tool not available
        elif tool_name.startswith("weather"):
            tool = self.tools.get("weather_tools")
            if tool:
                return tool._format_weather_response(data)
            return f"Weather data: {data}"
        elif tool_name.startswith("time"):
            tool = self.tools.get("time_tools")
            if tool:
                return tool._format_time_response(data)
            return f"Time data: {data}"
        elif tool_name.startswith("tweet"):
            return f"Tweet tool response: {data}"
        else:
            return f"{tool_name}: {data}"

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