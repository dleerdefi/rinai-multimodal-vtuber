from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any
import logging
import asyncio
import os
from dotenv import load_dotenv
from datetime import datetime, UTC, timedelta
import json
from pydantic import ValidationError
from src.tools.base import (
    AgentResult, 
    AgentDependencies, 
    TweetApprovalAnalysis,
    ToolCommand,
    TweetContent,
    TweetGenerationResponse,
    CommandAnalysis
)
from src.services.llm_service import LLMService, ModelType
from src.clients.perplexity_client import PerplexityClient
from src.clients.coingecko_client import CoinGeckoClient
from src.managers.tool_state_manager import ToolStateManager, TweetStatus, ToolOperationState
from bson.objectid import ObjectId
from src.db.mongo_manager import MongoManager
from src.db.db_schema import TweetStatus as DBTweetStatus, Tweet, TweetSchedule
from src.utils.json_parser import parse_strict_json
from src.utils.trigger_detector import TriggerDetector

load_dotenv()
logger = logging.getLogger(__name__)

class Orchestrator:
    """Core tool orchestrator"""
    
    def __init__(self, deps: Optional[AgentDependencies] = None):
        """Initialize orchestrator with optional dependencies"""
        self.llm_service = LLMService({
            "model_type": ModelType.GROQ_LLAMA_3_3_70B
        })
        
        # Initialize clients with proper error handling
        perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')
        if not perplexity_api_key:
            logger.warning("PERPLEXITY_API_KEY not found, web search will be disabled")
            self.perplexity = None
        else:
            self.perplexity = PerplexityClient(perplexity_api_key)
        
        coingecko_api_key = os.getenv('COINGECKO_API_KEY')
        if not coingecko_api_key:
            logger.warning("COINGECKO_API_KEY not found, crypto data will be disabled")
            self.coingecko = None
        else:
            self.coingecko = CoinGeckoClient(coingecko_api_key)
        
        # Initialize with default test dependencies if none provided
        self.deps = deps or AgentDependencies(
            conversation_id="test-convo-123",
            user_id="test-user-123",
            context={},
            tools_available=["crypto_data", "perplexity_search"]
        )
        
        # Get database instance and initialize tool state manager
        self.db = MongoManager.get_db()
        self.tool_state_manager = ToolStateManager(db=self.db)
        
    async def initialize(self):
        """Initialize async components"""
        # Initialize clients with context managers
        if self.perplexity:
            await self.perplexity.initialize()
        if self.coingecko:
            self.coingecko = await self.coingecko.__aenter__()
        
    async def cleanup(self):
        """Cleanup async resources"""
        if self.perplexity:
            await self.perplexity.close()
        if self.coingecko:
            await self.coingecko.__aexit__(None, None, None)
        
    async def process_command(self, command: str, deps: Optional[AgentDependencies] = None) -> AgentResult:
        """Process a command and execute required tools"""
        try:
            # Store deps if provided
            if deps:
                self.deps = deps
            
            trigger_detector = TriggerDetector()
            
            # First check if this is a direct tool request (crypto/perplexity)
            tool_type = trigger_detector.get_specific_tool_type(command)
            if tool_type in ["crypto_data", "perplexity_search"]:
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
                        approval_result = await self._process_tweet_approval_response(
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
            if trigger_detector.should_use_twitter(command):
                # Use specialized Twitter analysis
                return await self._analyze_twitter_command(command)
            
            prompt = f"""You are a tool orchestrator that carefully analyzes commands to determine if special tools are required.
DEFAULT BEHAVIOR: Most commands should return an empty tools array - tools are only used in specific cases.

Command: "{command}"

Available tools (use ONLY when specifically needed):
1. crypto_data: ONLY use when explicitly asking about cryptocurrency prices or market data
   Example: "What's Bitcoin's price?" or "Show me ETH market data"
   
2. perplexity_search: ONLY use for queries requiring current events or real-time information
   Example: "What happened at the latest Fed meeting?" or "What are today's top AI developments?"

Instructions:
- Default response should be empty tools array unless command CLEARLY requires a tool
- For most general conversation, return empty tools array
- Only use crypto_data for explicit cryptocurrency price/market requests
- Only use perplexity_search for queries needing current/real-time information
- Respond with valid JSON only

Example responses:

For general chat (MOST COMMON):
{{
    "tools_needed": [],
    "reasoning": "No special tools required for general conversation"
}}

For crypto price request:
{{
    "tools_needed": [
        {{
            "tool_name": "crypto_data",
            "action": "get_price",
            "parameters": {{"symbol": "BTC"}},
            "priority": 1
        }}
    ],
    "reasoning": "Explicit request for cryptocurrency price data"
}}

For current events:
{{
    "tools_needed": [
        {{
            "tool_name": "perplexity_search",
            "action": "search",
            "parameters": {{"query": "latest Fed meeting results"}},
            "priority": 1
        }}
    ],
    "reasoning": "Query demands current real-time information from the web"
}}"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a conservative tool orchestrator. Default to using NO tools unless explicitly required."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 500
                }
            )
            
            try:
                data = parse_strict_json(response, CommandAnalysis)
                if data:
                    return data
                else:
                    logger.debug("No tools needed - returning empty analysis")
                    return CommandAnalysis(
                        tools_needed=[],
                        reasoning="No special tools required"
                    )
                
            except (json.JSONDecodeError, ValidationError) as e:
                logger.error(f"Failed to parse LLM response: {response}")
                logger.error(f"Parse error: {str(e)}")
                return CommandAnalysis(
                    tools_needed=[],
                    reasoning="Failed to parse response, defaulting to no tools"
                )
            
        except Exception as e:
            logger.error(f"Error analyzing command: {e}", exc_info=True)
            return CommandAnalysis(
                tools_needed=[],
                reasoning="Error during analysis, defaulting to no tools"
            )
            
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
                            session_id = tool.parameters.get("session_id")
                            
                            # Create schedule info dictionary
                            schedule_info = {
                                "session_id": session_id,
                                "topic": tool.parameters.get("topic"),
                                "total_tweets": tool.parameters.get("tweet_count", 1),
                                "status": "pending",
                                "created_at": datetime.now(UTC),
                                "updated_at": datetime.now(UTC)
                            }
                            
                            # Create schedule first
                            schedule = await self.db.create_tweet_schedule(
                                session_id=session_id,
                                topic=tool.parameters.get("topic"),
                                total_tweets=tool.parameters.get("tweet_count", 1),
                                schedule_info=schedule_info  # Add the required schedule_info
                            )
                            
                            # Generate tweets
                            tweets = await self._generate_tweet_series(
                                topic=tool.parameters.get("topic"),
                                count=tool.parameters.get("tweet_count", 1),
                                tone=tool.parameters.get("tone", "professional"),
                                original_request=tool.parameters.get("original_request"),
                                session_id=session_id
                            )
                            
                            # Handle approval flow
                            if session_id:
                                approval_result = await self._handle_tweet_approval_flow(
                                    tweets=tweets["tweets"],
                                    session_id=session_id
                                )
                                results["twitter"] = approval_result
                            else:
                                results["twitter"] = {
                                    "status": "pending_approval",
                                    "content": tweets["tweets"],
                                    "schedule": tool.parameters,
                                    "timestamp": datetime.utcnow().isoformat()
                                }
                            continue  # Skip adding to tasks
                    elif tool.tool_name == "perplexity_search":
                        if self.perplexity:
                            tasks.append(self.perplexity.search(
                                query=tool.parameters.get("query", ""),
                                max_tokens=tool.parameters.get("max_tokens", 300)
                            ))
                        else:
                            results[tool.tool_name] = {
                                "status": "error",
                                "error": "Perplexity search is not configured",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                    elif tool.tool_name in ["crypto_data", "crypto_price"]:
                        if self.coingecko:
                            symbol = tool.parameters.get("symbol", "").upper()
                            include_details = tool.parameters.get("include_details", False)
                            tasks.append(self._get_crypto_data(symbol, include_details))
                        else:
                            results[tool.tool_name] = {
                                "status": "error",
                                "error": "CoinGecko is not configured",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                    elif tool.tool_name == "crypto_market":
                        symbol = tool.parameters.get("symbol", "").upper()
                        include_social = tool.parameters.get("include_social", True)
                        tasks.append(self._get_crypto_market_data(symbol, include_social))
                    
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

    async def _get_crypto_data(self, symbol: str, include_details: bool = False) -> Dict:
        """Get cryptocurrency data with proper session handling"""
        try:
            # Get CoinGecko ID
            coingecko_id = await self.coingecko._get_coingecko_id(symbol)
            if not coingecko_id:
                return {
                    "status": "error",
                    "error": f"Could not find CoinGecko ID for symbol: {symbol}",
                    "timestamp": datetime.utcnow().isoformat()
                }

            # Gather all requested data concurrently
            tasks = [self.coingecko.get_token_price(coingecko_id)]
            
            if include_details:
                tasks.extend([
                    self.coingecko.get_token_details(coingecko_id),
                    self.coingecko.get_market_chart(coingecko_id),
                    self.coingecko.get_trending(),
                    self.coingecko.get_global_data()
                ])

            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            data = {}
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Error in crypto data fetch: {result}")
                    continue
                if result:
                    data.update(result)

            return {
                "status": "success",
                "data": data,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"Error fetching crypto data for {symbol}: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _get_crypto_market_data(self, symbol: str, include_social: bool = True) -> Dict:
        """Get detailed cryptocurrency market data including social metrics"""
        try:
            coingecko_id = await self.coingecko._get_coingecko_id(symbol)
            if not coingecko_id:
                return {
                    "status": "error",
                    "error": f"Could not find CoinGecko ID for symbol: {symbol}"
                }
            
            details = await self.coingecko.get_token_details(coingecko_id)
            if not details:
                return {
                    "status": "error",
                    "error": f"Could not fetch market data for {symbol}"
                }
            
            # Filter social metrics if not requested
            if not include_social:
                details = {k: v for k, v in details.items() 
                          if not k in ['twitter_followers', 'reddit_subscribers', 
                                     'telegram_channel_user_count']}
            
            return {
                "status": "success",
                "data": details,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error fetching market data for {symbol}: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }
        
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
                # Handle TTS responses from tools
                if isinstance(result, dict):
                    if result.get("requires_tts"):
                        logger.info(f"Found TTS response from {tool_name}")
                        return result["response"]
                    elif result.get("response"):
                        logger.info(f"Found regular response from {tool_name}")
                        response.append(result["response"])
                    elif result.get("status") == "success":
                        if "data" in result:
                            response.append(self._format_tool_data(tool_name, result["data"]))
                else:
                    logger.warning(f"Unexpected result format from {tool_name}: {result}")
                    
            return "\n".join(response) if response else "I processed your request but didn't get a clear response. Could you try rephrasing?"
            
        except Exception as e:
            logger.error(f"Error formatting response: {e}", exc_info=True)
            return "I encountered an error processing the response. Could you try again?"
            
    def _format_tool_data(self, tool_name: str, data: Any) -> str:
        """Format specific tool data into readable response"""
        if tool_name.startswith("crypto"):
            return self._format_crypto_response(data)
        elif tool_name.startswith("tweet"):
            return f"Tweet tool response: {data}"
        else:
            return f"{tool_name}: {data}"

    def _format_crypto_response(self, data: Dict) -> str:
        """Format cryptocurrency data for Rin agent consumption"""
        try:
            response_parts = []
            
            # Basic price info
            if 'price_usd' in data:
                response_parts.append(f"💰 Current Price: ${data['price_usd']:,.2f} USD")
            
            # Price changes
            changes = {
                '24h': data.get('price_change_24h'),
                '7d': data.get('price_change_7d'),
                '30d': data.get('price_change_30d')
            }
            change_strs = []
            for period, change in changes.items():
                if change is not None:
                    emoji = "📈" if change > 0 else "📉"
                    change_strs.append(f"{emoji} {period}: {change:+.2f}%")
            if change_strs:
                response_parts.append("Price Changes:")
                response_parts.extend(change_strs)
            
            # Market data
            if 'market_cap' in data:
                response_parts.append(f"🌐 Market Cap: ${data['market_cap']:,.0f}")
            if 'total_volume' in data:
                response_parts.append(f"📊 24h Volume: ${data['total_volume']:,.0f}")
            
            # Supply info
            if any(k in data for k in ['circulating_supply', 'total_supply', 'max_supply']):
                response_parts.append("Supply Information:")
                if 'circulating_supply' in data:
                    response_parts.append(f"  • Circulating: {data['circulating_supply']:,.0f}")
                if 'total_supply' in data:
                    response_parts.append(f"  • Total: {data['total_supply']:,.0f}")
                if 'max_supply' in data:
                    response_parts.append(f"  • Max: {data['max_supply']:,.0f}")
            
            # Social metrics (if available)
            social_metrics = {
                'twitter_followers': '𝕏',
                'reddit_subscribers': '📱',
                'telegram_channel_user_count': '📢'
            }
            social_data = []
            for key, emoji in social_metrics.items():
                if data.get(key):
                    social_data.append(f"{emoji} {key.replace('_', ' ').title()}: {data[key]:,}")
            if social_data:
                response_parts.append("\nSocial Metrics:")
                response_parts.extend(social_data)
            
            return "\n".join(response_parts)
            
        except Exception as e:
            logger.error(f"Error formatting crypto response: {e}")
            return str(data)  # Fallback to basic string representation

    async def _analyze_twitter_command(self, command: str) -> CommandAnalysis:
        """Specialized analysis for Twitter commands"""
        try:
            # Get session_id from deps instead of command
            session_id = self.deps.conversation_id if self.deps else None
            
            # Start a tool operation for Twitter
            operation = await self.tool_state_manager.start_operation(
                session_id=session_id,
                operation_type="twitter",
                initial_data={
                    "command": command,
                    "status": TweetStatus.PENDING.value
                }
            )
            
            prompt = f"""You are a Twitter action analyzer. Determine the specific Twitter action needed.

Command: "{command}"

Available Twitter actions: 
1. send_tweet: Post a new tweet immediately
   Parameters: message, account_id (optional)

2. schedule_tweets: Schedule one or more tweets for later
   Parameters: 
   - tweet_count: number of tweets to schedule
   - topic: what to tweet about
   - schedule_type: "one_time"
   - schedule_time: when to post (default: spread over next 24 hours)
   - approval_required: true

Instructions:
- Return ONLY valid JSON matching the example format
- Extract count, topic, and timing information from command
- If no specific time mentioned, default to spreading tweets over next 24 hours
- Include schedule_time in parameters

Example response format:
{{
    "tools_needed": [{{
        "tool_name": "twitter",
        "action": "schedule_tweets",
        "parameters": {{
            "tweet_count": 5,
            "topic": "artificial intelligence",
            "schedule_type": "one_time",
            "schedule_time": "spread_24h",
            "approval_required": true
        }},
        "priority": 1
    }}],
    "reasoning": "User requested scheduling multiple tweets about AI"
}}"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a precise Twitter action analyzer. Return ONLY valid JSON with no additional text."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 200
                }
            )

            try:
                # Extract JSON if response contains extra text
                response = response.strip()
                start_idx = response.find('{')
                end_idx = response.rfind('}') + 1
                if start_idx != -1 and end_idx != 0:
                    json_str = response[start_idx:end_idx]
                    logger.debug(f"Extracted JSON string: {json_str}")
                    data = json.loads(json_str)
                    return CommandAnalysis(**data)
                else:
                    logger.error(f"No JSON found in response: {response}")
                    raise ValueError("No valid JSON found in response")
                    
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Failed to parse response: {response}")
                logger.error(f"Parse error: {str(e)}")
                raise

        except Exception as e:
            logger.error(f"Error in Twitter command analysis: {e}")
            if session_id:
                await self.tool_state_manager.end_operation(session_id, success=False)
            raise

    async def _generate_tweet_series(self, topic: str, count: int = 1, tone: str = "professional", 
                                   original_request: str = None, session_id: str = None) -> Dict:
        """Generate one or more tweets about a topic"""
        try:
            # Use session_id from deps if not provided
            current_session_id = session_id or (self.deps.conversation_id if self.deps else None)
            if not current_session_id:
                raise ValueError("No session_id available")

            # Create schedule first with proper positional arguments
            schedule_id = await self.db.create_tweet_schedule(
                session_id=current_session_id,
                topic=topic,
                total_tweets=count,
                schedule_info={
                    "topic": topic,
                    "total_tweets": count,
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "start_time": datetime.now(UTC),
                    "interval_minutes": 60,
                    "schedule_type": "one_time",
                    "metadata": {
                        "tone": tone,
                        "original_request": original_request
                    }
                }
            )

            # Generate tweets using existing logic
            response = await self.llm_service.get_response(
                prompt=[
                    {
                        "role": "system",
                        "content": "You are Rin, an AI VTuber who creates engaging tweets. Return ONLY valid JSON."
                    },
                    {
                        "role": "user",
                        "content": f"""Generate {'a single' if count == 1 else str(count)} engaging tweet{'s' if count > 1 else ''} about {topic}.
                        
Requirements:
- {'Each tweet must be' if count > 1 else 'Must be'} under 280 characters
- Maintain a {tone} tone
- Be engaging and natural
- Return as JSON: {{"tweets": [{{"content": "tweet text"}}]}}"""
                    }
                ],
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.7,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"}
                }
            )

            # Parse and validate tweets using parse_strict_json
            tweet_data = parse_strict_json(response, TweetGenerationResponse)
            if not tweet_data:
                logger.error("Failed to parse tweet generation response")
                raise ValueError("Failed to generate valid tweets")

            validated_tweets = []
            for tweet in tweet_data.tweets:
                if len(tweet.content) <= 280:
                    tweet_data = {
                        "content": tweet.content,
                        "metadata": {
                            "estimated_engagement": "medium",
                            "generated_at": datetime.utcnow().isoformat()
                        }
                    }
                    validated_tweets.append(tweet_data)

            if not validated_tweets:
                raise ValueError("No valid tweets generated")

            # Store tweets with schedule reference
            stored_tweet_ids = []
            for tweet in validated_tweets:
                tweet_id = await self.db.create_tweet(
                    content=tweet["content"],
                    schedule_id=schedule_id,
                    session_id=current_session_id
                )
                stored_tweet_ids.append(tweet_id)

            # Update schedule with pending tweet IDs
            await self.db.update_tweet_schedule(
                schedule_id=schedule_id,
                pending_tweet_ids=stored_tweet_ids,
                status="collecting_approval"
            )

            return {
                "schedule_id": schedule_id,
                "tweets": validated_tweets,
                "stored_tweet_ids": stored_tweet_ids
            }

        except Exception as e:
            logger.error(f"Error generating tweet series: {e}")
            raise

    async def _store_approved_tweets(self, tweets: List[Dict], schedule_info: Dict) -> str:
        """Store approved tweets using RinDB schema"""
        db = MongoManager.get_db()
        
        # Create or update tweet schedule
        schedule_id = await db.create_tweet_schedule(
            session_id=schedule_info.get('session_id'),
            topic=schedule_info.get('topic', 'general'),
            total_tweets=len(tweets),
            schedule_info=schedule_info
        )
        
        # Store individual tweets
        for tweet in tweets:
            await db.create_tweet(
                content=tweet['content'],
                schedule_id=schedule_id,
                session_id=schedule_info.get('session_id'),
                scheduled_time=tweet.get('scheduled_time')
            )
            
        return schedule_id

    async def _execute_tweet(self, tweet_data: Dict) -> Dict:
        """Execute a single tweet using TwitterAgentClient"""
        try:
            result = await self.twitter_client.send_tweet(**tweet_data["twitter_api_params"])
            return {
                "status": "success",
                "tweet_id": result.get("id"),
                "timestamp": datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error posting tweet: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _handle_tweet_approval_flow(self, tweets: List[Dict], session_id: str, approved_tweets: List[Dict] = None) -> Dict:
        """Handle tweet approval flow with improved analysis"""
        try:
            db = MongoManager.get_db()
            
            # Update operation state to collecting approvals
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                state=ToolOperationState.COLLECTING,
                step="awaiting_approval"
            )
            
            # Get existing schedule or create new one
            schedule = await db.get_session_tweet_schedule(session_id)
            if not schedule:
                return {
                    "status": "error",
                    "response": "Could not find active tweet schedule.",
                    "requires_tts": True
                }
            
            # Get metadata from both possible locations in schedule
            schedule_info = schedule.get("schedule_info", {})
            schedule_metadata = schedule.get("metadata", {}) or {}  # Ensure it's never None

            # Get nested metadata from schedule_info safely
            schedule_info_metadata = schedule_info.get("metadata", {}) or {}

            # Combine them with schedule_info taking precedence
            tone = (
                schedule_info_metadata.get("tone") or 
                schedule_metadata.get("tone") or 
                "professional"
            )
            original_request = (
                schedule_info_metadata.get("original_request") or 
                schedule_metadata.get("original_request")
            )
            
            # Use combined analysis for initial quality check
            analysis = await self._analyze_tweet_quality(
                tweets=tweets,
                metadata={"topic": schedule.get("topic", "general")}
            )
            
            if analysis["quality_check"] == "needs_improvement":
                return {
                    "status": "regenerate_all",
                    "response": f"{analysis['feedback']}\n\nSuggested improvements:\n" + "\n".join(analysis['suggestions']),
                    "requires_tts": True
                }
            
            # Quality passed, update schedule with pending tweets
            await db.update_tweet_schedule(
                schedule_id=str(schedule["_id"]),
                pending_tweet_ids=[],  # Will be filled after tweet creation
                status="collecting_approval"
            )
            
            # Store pending tweets
            for tweet in tweets:
                await db.create_tweet(
                    content=tweet["content"],
                    schedule_id=str(schedule["_id"]),
                    session_id=session_id,
                    status=TweetStatus.PENDING.value
                )

            # Format TTS response based on analysis
            tts_response = (
                f"{analysis['feedback']}\n\n"
                f"Would you like to:\n"
                "1. Approve all tweets\n"
                "2. Approve specific tweets\n"
                "3. Request changes\n"
                f"We need {schedule['total_tweets_requested'] - len(approved_tweets or [])} more tweet(s) to complete the schedule."
            )

            return {
                "status": "awaiting_approval",
                "response": tts_response,
                "requires_tts": True,
                "data": {
                    "tweets": tweets,
                    "analysis": analysis,
                    "remaining": schedule['total_tweets_requested'] - len(approved_tweets or [])
                }
            }

        except Exception as e:
            logger.error(f"Error in tweet approval flow: {e}")
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                state=ToolOperationState.ERROR,
                step="approval_flow_error"
            )
            return {
                "status": "error",
                "response": "I encountered an error analyzing the tweets. Would you like me to try again?",
                "requires_tts": True
            }

    def _format_tweets_for_presentation(self, tweets: List[Dict]) -> str:
        """Format tweets for TTS-friendly presentation"""
        formatted = []
        for i, tweet in enumerate(tweets, 1):
            formatted.append(f"Tweet {i}:\n{tweet['content']}\n")
        return "\n".join(formatted)

    async def _process_tweet_approval_response(self, message: str, session_id: str) -> Dict:
        """Process user's response to tweet approval"""
        try:
            # Get operation state first
            operation_state = await self.tool_state_manager.get_operation_state(session_id)
            logger.debug(f"Current operation state: {operation_state}")
            
            # If tweets are already approved or scheduled, don't process any more commands
            if operation_state and operation_state.get("status") in ["approved", "scheduled"]:
                return {
                    "status": "success",
                    "response": "Your tweets are already scheduled and being processed. You can safely exit.",
                    "requires_tts": True
                }
            
            # More lenient state validation - accept any collecting state
            if not operation_state or operation_state.get('state') != 'collecting':
                logger.warning(f"Invalid state for tweet approval: {operation_state}")
                return {
                    "status": "error",
                    "response": "I've lost track of our tweet approval process. Would you like to start over?",
                    "requires_tts": True
                }

            # Check for exit commands first
            exit_keywords = ["stop", "cancel", "exit", "quit", "end", "terminate"]
            if any(keyword in message.lower() for keyword in exit_keywords):
                logger.info(f"Exit command detected: {message}")
                await self.tool_state_manager.end_operation(
                    session_id=session_id,
                    success=False,
                    reason="User requested to stop"
                )
                return {
                    "status": "cancelled",
                    "response": "I've stopped the tweet process. Let me know if you need anything else!",
                    "requires_tts": True
                }

            # Get active schedule and pending tweets
            db = MongoManager.get_db()
            schedule = await db.get_session_tweet_schedule(session_id)
            if not schedule:
                logger.error("No active schedule found")
                return {
                    "status": "error",
                    "response": "I couldn't find the tweets we were discussing.",
                    "requires_tts": True
                }

            # Get ONLY the most recent pending tweets
            pending_tweets = await db.get_tweets_by_schedule(str(schedule["_id"]))
            pending_tweets = [t for t in pending_tweets if t["status"] == TweetStatus.PENDING.value]
            logger.info(f"Found {len(pending_tweets)} pending tweets")

            # Analyze user response with LLM
            analysis = await self._analyze_tweets_and_response(
                tweets=pending_tweets,
                user_response=message,
                metadata={"topic": schedule.get("topic", "general")}
            )
            logger.info(f"Analysis result: {analysis}")

            try:
                # Access Pydantic model attributes with dot notation
                if analysis.action == "full_approval":
                    # Update all pending tweets to approved
                    for tweet in pending_tweets:
                        await db.update_tweet_status(
                            tweet_id=str(tweet["_id"]),
                            status=TweetStatus.APPROVED.value
                        )
                    
                    # Update schedule status
                    await db.update_tweet_schedule(
                        schedule_id=str(schedule["_id"]),
                        status="ready_to_schedule"
                    )
                    
                    # Activate the schedule
                    schedule_activated = await self._activate_tweet_schedule(
                        str(schedule["_id"]), 
                        schedule["schedule_info"]
                    )
                    
                    if schedule_activated:
                        return {
                            "status": "completed",
                            "response": analysis.feedback,
                            "requires_tts": True
                        }

                elif analysis.action in ["partial_approval", "partial_regenerate"]:
                    # Update operation state to track partial approval
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        state=ToolOperationState.COLLECTING,
                        step="partial_approval_in_progress",
                        data={
                            "schedule_id": str(schedule["_id"]),
                            "approved_indices": analysis.approved_indices,
                            "pending_indices": analysis.regenerate_indices,
                            "total_needed": schedule["total_tweets_requested"]
                        }
                    )

                    # Process approved tweets first
                    for idx in analysis.approved_indices:
                        if 0 <= idx - 1 < len(pending_tweets):
                            await db.update_tweet_status(
                                tweet_id=str(pending_tweets[idx - 1]["_id"]),
                                status=TweetStatus.APPROVED.value
                            )
                    
                    # Mark and regenerate rejected tweets immediately
                    rejected_count = len(analysis.regenerate_indices)
                    if rejected_count > 0:
                        # Mark tweets as rejected
                        for idx in analysis.regenerate_indices:
                            if 0 <= idx - 1 < len(pending_tweets):
                                await db.update_tweet_status(
                                    tweet_id=str(pending_tweets[idx - 1]["_id"]),
                                    status=TweetStatus.REJECTED.value
                                )
                        
                        # Get metadata from both possible locations in schedule
                        schedule_info = schedule.get("schedule_info", {})
                        schedule_metadata = schedule.get("metadata", {}) or {}  # Ensure it's never None

                        # Get nested metadata from schedule_info safely
                        schedule_info_metadata = schedule_info.get("metadata", {}) or {}

                        # Combine them with schedule_info taking precedence
                        tone = (
                            schedule_info_metadata.get("tone") or 
                            schedule_metadata.get("tone") or 
                            "professional"
                        )
                        original_request = (
                            schedule_info_metadata.get("original_request") or 
                            schedule_metadata.get("original_request")
                        )
                        
                        # Generate new tweets with proper metadata handling
                        new_tweets = await self._generate_tweet_series(
                            topic=schedule["topic"],
                            count=rejected_count,
                            tone=tone,
                            original_request=original_request,
                            session_id=session_id
                        )
                        
                        # Store new tweets with metadata
                        stored_tweet_ids = []
                        for tweet in new_tweets["tweets"]:
                            # Create tweet with basic parameters
                            tweet_id = await db.create_tweet(
                                content=tweet["content"],
                                schedule_id=str(schedule["_id"]),
                                session_id=session_id
                            )
                            
                            # Update the tweet's metadata separately
                            await db.update_tweet_status(
                                tweet_id=tweet_id,
                                status=TweetStatus.PENDING,
                                metadata={
                                    "original_request": original_request,
                                    "tone": tone,
                                    "generated_at": datetime.now(UTC).isoformat()
                                }
                            )
                            stored_tweet_ids.append(tweet_id)
                        
                        return {
                            "status": "partial_regenerated",
                            "response": f"{analysis.feedback}\nI've kept the approved tweets and generated new ones to replace the others. Here are the new tweets:\n{self._format_tweets_for_presentation(new_tweets['tweets'])}",
                            "requires_tts": True,
                            "data": {
                                "new_tweets": new_tweets,
                                "stored_tweet_ids": stored_tweet_ids,
                                "regenerate_count": rejected_count
                            }
                        }

                elif analysis.action == "regenerate_all":
                    logger.info("Regenerating all tweets")
                    # Mark existing tweets as rejected
                    for tweet in pending_tweets:
                        await db.update_tweet_status(
                            tweet_id=str(tweet["_id"]),
                            status=TweetStatus.REJECTED.value
                        )
                    
                    # Get metadata from both possible locations in schedule
                    schedule_info = schedule.get("schedule_info", {})
                    schedule_metadata = schedule.get("metadata", {}) or {}  # Ensure it's never None

                    # Get nested metadata from schedule_info safely
                    schedule_info_metadata = schedule_info.get("metadata", {}) or {}

                    # Combine them with schedule_info taking precedence
                    tone = (
                        schedule_info_metadata.get("tone") or 
                        schedule_metadata.get("tone") or 
                        "professional"
                    )
                    original_request = (
                        schedule_info_metadata.get("original_request") or 
                        schedule_metadata.get("original_request")
                    )
                    
                    # Generate new tweets with proper metadata handling
                    new_tweets = await self._generate_tweet_series(
                        topic=schedule["topic"],
                        count=schedule["total_tweets_requested"],
                        tone=tone,
                        original_request=original_request,
                        session_id=session_id
                    )
                    
                    # Store new tweets with metadata
                    stored_tweet_ids = []
                    for tweet in new_tweets["tweets"]:
                        tweet_id = await db.create_tweet(
                            content=tweet["content"],
                            schedule_id=str(schedule["_id"]),
                            session_id=session_id,
                            status=TweetStatus.PENDING.value,
                            metadata={
                                "original_request": original_request,
                                "tone": tone,
                                "generated_at": datetime.now(UTC).isoformat()
                            }
                        )
                        stored_tweet_ids.append(tweet_id)
                    
                    # Update schedule
                    await db.update_tweet_schedule(
                        schedule_id=str(schedule["_id"]),
                        pending_tweet_ids=stored_tweet_ids,
                        status="collecting_approval"
                    )
                    
                    return {
                        "status": "regenerated",
                        "response": f"I've generated new tweets for you. Here they are:\n{self._format_tweets_for_presentation(new_tweets['tweets'])}",
                        "requires_tts": True,
                        "data": {"tweets": new_tweets}
                    }

                # Check remaining tweets needed
                approved_tweets = await db.get_tweets_by_schedule(str(schedule["_id"]))
                approved_count = sum(1 for t in approved_tweets if t["status"] == TweetStatus.APPROVED.value)
                remaining = schedule["total_tweets_requested"] - approved_count

                if remaining > 0:
                    return {
                        "status": "in_progress",
                        "response": f"{analysis.feedback}\nWe still need {remaining} more tweets. Would you like me to generate them now?",
                        "requires_tts": True
                    }

                return {
                    "status": "awaiting_input",
                    "response": f"{analysis.feedback}\nWhat would you like me to do with these tweets?",
                    "requires_tts": True
                }

            except Exception as e:
                logger.error(f"Error in approval action processing: {e}", exc_info=True)
                return {
                    "status": "error",
                    "response": "I had trouble processing your approval. Would you like to try again?",
                    "requires_tts": True
                }

        except Exception as e:
            logger.error(f"Error processing approval: {e}", exc_info=True)
            # ... outer exception handling ...

    async def _activate_tweet_schedule(self, schedule_id: str, schedule_info: Dict) -> bool:
        """Activate a tweet schedule after approval"""
        try:
            # Get all approved tweets for this schedule
            tweets = await self.db.get_tweets_by_schedule(schedule_id)
            approved_tweets = [t for t in tweets if t["status"] == TweetStatus.APPROVED.value]
            
            # Get or create start_time
            start_time = schedule_info.get("start_time")
            if not start_time:
                start_time = datetime.now(UTC)
                logger.info(f"No start_time found, using current time: {start_time}")
            elif isinstance(start_time, str):
                start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                logger.info(f"Parsed start_time from string: {start_time}")
            
            interval_minutes = schedule_info.get("interval_minutes", 2)
            interval = timedelta(minutes=interval_minutes)
            
            logger.info(f"Scheduling {len(approved_tweets)} tweets starting at {start_time} with {interval_minutes} minute intervals")
            
            # Update each tweet with its schedule time
            for i, tweet in enumerate(approved_tweets):
                scheduled_time = start_time + (interval * i)
                # Update tweet status and store scheduled_time in metadata
                await self.db.update_tweet_status(
                    tweet_id=str(tweet["_id"]),
                    status=TweetStatus.SCHEDULED.value,
                    metadata={
                        "scheduled_time": scheduled_time.isoformat(),
                        "schedule_index": i
                    }
                )
                logger.info(f"Scheduled tweet {tweet['_id']} for {scheduled_time}")
            
            # Update schedule status with proper datetime handling
            await self.db.update_tweet_schedule(
                schedule_id=schedule_id,
                status="scheduled",
                schedule_info={
                    **schedule_info,
                    "start_time": start_time.isoformat(),
                    "interval_minutes": interval_minutes,
                    "last_updated": datetime.now(UTC).isoformat()
                }
            )
            
            logger.info(f"Successfully activated schedule {schedule_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error activating tweet schedule: {e}", exc_info=True)
            return False

    async def _analyze_tweets_and_response(self, tweets: List[Dict], user_response: str, metadata: Dict = None) -> Dict:
        """Analyze user's response to tweets"""
        try:
            presentation = self._format_tweets_for_presentation(tweets)
            
            prompt = [
                {
                    "role": "system",
                    "content": "Analyze user instructions on how to proceed with the proposed draft tweet(s) and return structured JSON."
                },
                {
                    "role": "user",
                    "content": f"""Context: Previous tweets presented:
{presentation}

User response: "{user_response}"

There are {len(tweets)} tweets to analyze. Return ONLY valid JSON in this exact format:
{{
    "action": "full_approval" | "partial_approval" | "regenerate_all" | "partial_regenerate",
    "approved_indices": [list of approved tweet numbers from 1 to {len(tweets)}],
    "regenerate_indices": [list of tweet numbers to regenerate from 1 to {len(tweets)}],
    "feedback": "explanation in Rin's voice"
}}"""
                }
            ]

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"}
                }
            )

            # Use the new parse_approval_response method
            return await self._parse_approval_response(response)

        except Exception as e:
            logger.error(f"Error in tweet response analysis: {e}")
            raise

    async def _parse_approval_response(self, response: str) -> Dict:
        """Parse the approval response from the LLM"""
        try:
            # Use existing parse_strict_json from json_parser
            data = parse_strict_json(response, TweetApprovalAnalysis)
            
            if not data:
                logger.warning("Failed to parse approval response, using default regenerate")
                return {
                    'action': 'regenerate_all',
                    'approved_indices': [],
                    'regenerate_indices': [1, 2],
                    'feedback': "I'll generate new tweets for you."
                }
                
            return data

        except Exception as e:
            logger.error(f"Failed to parse approval response: {e}")
            return {
                'action': 'regenerate_all',
                'approved_indices': [],
                'regenerate_indices': [1, 2],
                'feedback': "I'll generate new tweets for you."
            }

    async def _get_db(self):
        """Get database instance"""
        return MongoManager.get_db()

    async def _analyze_tweet_quality(self, tweets: List[Dict], metadata: Dict = None) -> Dict:
        """Initial quality check of generated tweets before showing to user"""
        try:
            presentation = self._format_tweets_for_presentation(tweets)
            
            prompt = [
                {
                    "role": "system",
                    "content": "You are a professional tweet creator. Analyze tweet quality and return structured JSON."
                },
                {
                    "role": "user",
                    "content": f"""Analyze these tweets for a {metadata.get('topic', 'general')} thread:
{presentation}

Return ONLY valid JSON in this exact format:
{{
    "quality_check": "pass" | "needs_improvement",
    "feedback": "detailed feedback in Rin's voice",
    "suggestions": ["specific improvement points"]
}}"""
                }
            ]

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.2,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"}
                }
            )

            return json.loads(response)

        except Exception as e:
            logger.error(f"Error in tweet quality analysis: {e}")
            return {
                "quality_check": "needs_improvement",
                "feedback": "I had trouble analyzing the tweet quality. Let me generate new ones to be safe.",
                "suggestions": ["Generate new tweets with better quality assurance"]
            }

    async def _validate_content_alignment(self, original_query: str, generated_content: List[Dict], topic: str) -> Dict:
        """Validate if generated content aligns with the original query"""
        try:
            prompt = [
                {
                    "role": "system",
                    "content": "You are a professional tweet creator. Analyze if the generated content matches the user's request."
                },

                {
                    "role": "user",
                    "content": f"""
Original Request: "{original_query}"
Topic: {topic}

Generated Content:
{self._format_tweets_for_presentation(generated_content)}

Return ONLY valid JSON in this format:
{{
    "is_aligned": true/false,
    "reason": "brief explanation in Rin's voice",
    "suggestions": ["improvement suggestions if needed"],
    "severity": "low" | "medium" | "high"  // how badly misaligned
}}"""
                }
            ]

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.2,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"}
                }
            )

            validation = json.loads(response)
            logger.info(f"Content validation result: {validation}")
            return validation

        except Exception as e:
            logger.error(f"Error in content validation: {e}")
            return {
                "is_aligned": False,
                "reason": "Validation check failed",
                "suggestions": ["Please try regenerating the content"],
                "severity": "high"
            }

    async def _validate_command_analysis(self, analysis_result: Dict, current_state: Dict) -> Dict:
        """Validate if the command analysis aligns with the current system state"""
        try:
            # Extract current state information
            actual_tweet_count = len(current_state.get("pending_tweets", []))
            original_topic = current_state.get("topic")
            original_request = current_state.get("original_request")

            # Extract analyzed parameters
            analyzed_params = analysis_result.get("tools_needed", [{}])[0].get("parameters", {})
            
            # Validate key parameters
            mismatches = []
            if analyzed_params.get("tweet_count") != actual_tweet_count:
                mismatches.append({
                    "field": "tweet_count",
                    "expected": actual_tweet_count,
                    "received": analyzed_params.get("tweet_count"),
                    "severity": "high"
                })
            
            if analyzed_params.get("topic") != original_topic:
                mismatches.append({
                    "field": "topic",
                    "expected": original_topic,
                    "received": analyzed_params.get("topic"),
                    "severity": "high"
                })

            # If mismatches found, correct the analysis
            if mismatches:
                logger.warning(f"Command analysis mismatches found: {mismatches}")
                # Create corrected version of the command
                corrected_command = {
                    "tools_needed": [{
                        "tool_name": "twitter",
                        "action": "schedule_tweets",
                        "parameters": {
                            "tweet_count": actual_tweet_count,
                            "topic": original_topic,
                            "schedule_type": analyzed_params.get("schedule_type", "one_time"),
                            "schedule_time": analyzed_params.get("schedule_time", "now"),
                            "approval_required": True
                        },
                        "priority": 1
                    }],
                    "reasoning": f"Corrected analysis for scheduling {actual_tweet_count} tweets about {original_topic}"
                }
                return {
                    "is_valid": False,
                    "corrected_analysis": corrected_command,
                    "mismatches": mismatches
                }
            
            return {
                "is_valid": True,
                "original_analysis": analysis_result
            }

        except Exception as e:
            logger.error(f"Error validating command analysis: {e}")
            return {
                "is_valid": False,
                "error": str(e),
                "severity": "high"
            }