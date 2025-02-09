from pydantic import BaseModel, Field
from typing import List, Dict, Optional
import logging
import asyncio
import os
from dotenv import load_dotenv
from datetime import datetime
import json
from pydantic import ValidationError
from src.tools.base import AgentResult, AgentDependencies
from src.services.llm_service import LLMService, ModelType
from src.clients.perplexity_client import PerplexityClient
from src.clients.coingecko_client import CoinGeckoClient

load_dotenv()
logger = logging.getLogger(__name__)

class ToolCommand(BaseModel):
    """Structure for tool commands"""
    tool_name: str = Field(description="Name of tool to execute")
    action: str = Field(description="Action to perform")
    parameters: Dict = Field(default={}, description="Tool parameters")
    priority: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Execution priority (1-5)"
    )

class CommandAnalysis(BaseModel):
    """AI model for analyzing commands"""
    tools_needed: List[ToolCommand] = Field(description="Tools required for this command")
    reasoning: str = Field(description="Explanation of tool selection")

class Orchestrator:
    """Core tool orchestrator powered by Groq"""
    
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
        
    async def process_command(self, 
        command: str, 
        deps: Optional[AgentDependencies] = None,
        timeout: float = 25.0
    ) -> AgentResult:
        """Process and orchestrate tool execution with timeout"""
        if deps:
            self.deps = deps
            
        try:
            # Initialize resources for this command
            await self.initialize()
            
            try:
                # First, analyze command using Groq (with timeout)
                analysis = await asyncio.wait_for(
                    self._analyze_command(command), 
                    timeout=8.0
                )

                # Execute tools based on analysis (with timeout)
                results = await asyncio.wait_for(
                    self._execute_tools(analysis.tools_needed),
                    timeout=timeout
                )

                result = AgentResult(
                    response=self._format_response(results),
                    data=results
                )
                
                # Ensure we're done processing before returning
                await asyncio.sleep(0)  # Yield control to event loop
                return result

            except asyncio.TimeoutError:
                logger.warning(f"Tool execution timed out after {timeout} seconds")
                return AgentResult(
                    response="I apologize, but I couldn't fetch the latest information in time. Would you like to try again?",
                    data={"error": "Tool execution timeout"}
                )
                
            finally:
                # Ensure cleanup happens after command processing
                await self.cleanup()
                logger.info("Tool execution completed and resources cleaned up")
                
        except Exception as e:
            logger.error(f"Error in process_command: {e}", exc_info=True)
            return AgentResult(
                response="I encountered an error while processing your request. Please try again.",
                data={"error": str(e)}
            )
        
    async def _analyze_command(self, command: str) -> CommandAnalysis:
        """Analyze command to determine required tools"""
        try:
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

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 500
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
                    logger.debug("No tools needed - returning empty analysis")
                    return CommandAnalysis(
                        tools_needed=[],
                        reasoning="No special tools required"
                    )
                
            except (json.JSONDecodeError, ValidationError) as e:
                logger.error(f"Failed to parse LLM response: {response}")
                logger.error(f"Parse error: {str(e)}")
                # Default to no tools
                return CommandAnalysis(
                    tools_needed=[],
                    reasoning="Failed to parse response, defaulting to no tools"
                )
            
        except Exception as e:
            logger.error(f"Error analyzing command: {e}", exc_info=True)
            raise
            
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
                            # Handle scheduling separately since it needs LLM interaction
                            tweets = await self._generate_tweet_series(
                                topic=tool.parameters.get("topic"),
                                count=tool.parameters.get("tweet_count", 1),
                                tone=tool.parameters.get("tone", "professional")
                            )
                            results["twitter"] = {
                                "status": "pending_approval",
                                "content": tweets,
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
        response = []
        
        for tool_name, result in results.items():
            if result.get("status") == "success":
                data = result['data']
                if tool_name.startswith("crypto"):
                    response.append(self._format_crypto_response(data))
                else:
                    response.append(f"{tool_name}: {data}")
            else:
                response.append(f"❌ {tool_name} error: {result.get('error', 'Unknown error')}")
                
        return "\n".join(response)

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
        prompt = f"""You are a Twitter action analyzer. Determine the specific Twitter action needed.

Command: "{command}"

Available Twitter actions: 
1. send_tweet: Post a new tweet immediately
   Parameters: message, account_id (optional)

2. schedule_tweets: Schedule one or more tweets for later
   Parameters: 
   - tweets: array of tweet content
   - schedule_type: "one_time" or "recurring"
   - schedule_times: array of ISO datetime strings or cron expressions
   - approval_required: boolean (always true for multiple tweets)

Instructions:
- Determine if this is an immediate or scheduled action
- For scheduled tweets, extract timing and content requirements
- Return valid JSON matching CommandAnalysis structure

Example responses:

For immediate tweet:
{{
    "tools_needed": [{
        "tool_name": "twitter",
        "action": "send_tweet",
        "parameters": {{
            "message": "Hello Twitter!",
            "account_id": "default"
        }},
        "priority": 1
    }}],
    "reasoning": "User requested immediate tweet"
}}

For scheduled tweets:
{{
    "tools_needed": [{
        "tool_name": "twitter",
        "action": "schedule_tweets",
        "parameters": {{
            "tweet_count": 5,
            "topic": "bacon",
            "schedule_type": "one_time",
            "schedule_times": ["2024-03-21T10:00:00Z", "2024-03-21T13:00:00Z", ...],
            "approval_required": true
        }},
        "priority": 1
    }}],
    "reasoning": "User requested scheduling multiple tweets about bacon"
}}"""

        messages = [
            {
                "role": "system",
                "content": "You are a precise Twitter action analyzer. Only return valid actions with required parameters."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        # Use a more focused model for Twitter analysis
        response = await self.llm_service.get_response(
            prompt=messages,
            model_type=ModelType.GROQ_LLAMA_3_3_70B,
            override_config={
                "temperature": 0.1,
                "max_tokens": 500
            }
        )

        # Parse response and return CommandAnalysis
        try:
            data = json.loads(response)
            return CommandAnalysis(**data)
        except Exception as e:
            logger.error(f"Error parsing Twitter analysis: {e}")
            return CommandAnalysis(
                tools_needed=[],
                reasoning="Failed to parse Twitter action"
            )

    async def _generate_tweet_series(self, topic: str, count: int = 1, tone: str = "professional") -> List[Dict]:
        """Generate one or more tweets about a topic"""
        try:
            # Adjust prompt based on count
            if count == 1:
                prompt = f"""Generate a single engaging tweet about {topic}.
                
Requirements:
- Must be under 280 characters
- Maintain a {tone} tone
- Be engaging and natural"""
            else:
                prompt = f"""Generate {count} unique tweets about {topic}.
                
Requirements:
- Each tweet must be under 280 characters
- Maintain a {tone} tone
- Make each tweet unique and engaging
- Space content appropriately across the series"""

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.7,
                    "max_tokens": 1000
                }
            )

            try:
                # Handle single tweet case
                if count == 1:
                    tweet_content = response.strip()
                    tweets = [{
                        "content": tweet_content,
                        "estimated_engagement": "medium"
                    }]
                else:
                    tweets = json.loads(response)

                # Validate and format tweets
                validated_tweets = []
                for tweet in tweets:
                    if len(tweet["content"]) <= 280:
                        tweet_data = {
                            "content": tweet["content"],
                            "metadata": {
                                "estimated_engagement": tweet.get("estimated_engagement", "medium"),
                                "generated_at": datetime.utcnow().isoformat()
                            },
                            "twitter_api_params": {
                                "message": tweet["content"],
                                "account_id": "default",  # Can be overridden later
                                "media_files": None,  # For future media support
                                "poll_options": None  # For future poll support
                            }
                        }
                        validated_tweets.append(tweet_data)
                
                return validated_tweets

            except json.JSONDecodeError:
                logger.error("Failed to parse generated tweets")
                return []

        except Exception as e:
            logger.error(f"Error generating tweets: {e}")
            return []

    async def _store_approved_tweets(self, tweets: List[Dict], schedule_info: Dict) -> str:
        """Store approved tweets in MongoDB for scheduling"""
        try:
            schedule_data = {
                "tweets": tweets,
                "schedule_info": schedule_info,
                "status": "approved",
                "created_at": datetime.utcnow(),
                "last_updated": datetime.utcnow()
            }
            
            result = await self.db.scheduled_tweets.insert_one(schedule_data)
            return str(result.inserted_id)

        except Exception as e:
            logger.error(f"Error storing approved tweets: {e}")
            raise

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
        """Handle the tweet approval conversation flow with partial approvals"""
        try:
            # Track approved tweets across conversation turns
            if approved_tweets is None:
                approved_tweets = []

            # Format tweets for TTS presentation
            presentation = self._format_tweets_for_presentation(tweets)
            
            total_needed = metadata.get("total_tweets_requested", len(tweets))
            remaining = total_needed - len(approved_tweets)
            
            response = (
                f"I've generated {len(tweets)} tweet{'' if len(tweets) == 1 else 's'} "
                f"({remaining} more needed to complete your request). Here they are:\n\n"
                f"{presentation}\n\n"
                "You can:\n"
                "1. Say 'approve all' to keep all tweets\n"
                "2. Say 'keep tweet X' to approve specific tweets\n"
                "3. Say 'regenerate' to redo all of them\n"
                "4. Specify which tweets to regenerate (e.g., 'redo tweets 1, 3, and 5')"
            )

            # Store state in session metadata
            await self.db.update_session_metadata(session_id, {
                "pending_tweets": tweets,
                "approved_tweets": approved_tweets,
                "total_tweets_requested": total_needed,
                "approval_state": "awaiting_response",
                "last_action": "presented_tweets"
            })

            return {
                "status": "awaiting_approval",
                "response": response,
                "tweets": tweets
            }

        except Exception as e:
            logger.error(f"Error in tweet approval flow: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    def _format_tweets_for_presentation(self, tweets: List[Dict]) -> str:
        """Format tweets for TTS-friendly presentation"""
        formatted = []
        for i, tweet in enumerate(tweets, 1):
            formatted.append(f"Tweet {i}:\n{tweet['content']}\n")
        return "\n".join(formatted)

    async def _process_tweet_approval_response(self, message: str, session_id: str) -> Dict:
        """Process user's response to tweet approval with partial approval support"""
        try:
            # Get session metadata
            metadata = await self.db.get_session_metadata(session_id)
            if not metadata or "pending_tweets" not in metadata:
                return {
                    "status": "error",
                    "response": "I couldn't find the tweets we were discussing."
                }

            pending_tweets = metadata["pending_tweets"]
            approved_tweets = metadata.get("approved_tweets", [])
            total_needed = metadata.get("total_tweets_requested")

            # Analyze response for partial approvals
            approval_analysis = await self.llm_service.get_response(
                prompt=[{
                    "role": "system",
                    "content": """Analyze tweet approval response. Determine:
                    1. If it's a full approval
                    2. Which specific tweets are approved (by number)
                    3. Which tweets need regeneration
                    Return as JSON with format:
                    {
                        "action": "full_approval" | "partial_approval" | "regenerate_all" | "partial_regenerate",
                        "approved_indices": [1, 2, ...],
                        "regenerate_indices": [3, 4, ...]
                    }"""
                }, {
                    "role": "user",
                    "content": message
                }],
                model_type=ModelType.GROQ_LLAMA_3_3_70B
            )

            try:
                analysis = json.loads(approval_analysis)
                
                if analysis["action"] == "full_approval":
                    approved_tweets.extend(pending_tweets)
                
                elif analysis["action"] in ["partial_approval", "partial_regenerate"]:
                    # Add approved tweets to our collection
                    for idx in analysis["approved_indices"]:
                        if 0 <= idx - 1 < len(pending_tweets):  # Convert to 0-based index
                            approved_tweets.append(pending_tweets[idx - 1])
                    
                    # Generate new tweets for rejected ones
                    if analysis.get("regenerate_indices"):
                        num_to_regenerate = len(analysis["regenerate_indices"])
                        new_tweets = await self._generate_tweet_series(
                            topic=metadata.get("topic"),
                            count=num_to_regenerate
                        )
                        
                        # Start new approval flow with new tweets
                        return await self._handle_tweet_approval_flow(
                            tweets=new_tweets,
                            session_id=session_id,
                            approved_tweets=approved_tweets
                        )

                elif analysis["action"] == "regenerate_all":
                    new_tweets = await self._generate_tweet_series(
                        topic=metadata.get("topic"),
                        count=total_needed - len(approved_tweets)
                    )
                    return await self._handle_tweet_approval_flow(
                        tweets=new_tweets,
                        session_id=session_id,
                        approved_tweets=approved_tweets
                    )

                # Check if we have all needed tweets
                if len(approved_tweets) >= total_needed:
                    # Store final approved tweets
                    schedule_id = await self._store_approved_tweets(
                        approved_tweets[:total_needed],  # Only take what we need
                        metadata.get("schedule_info", {})
                    )
                    
                    response = (
                        "Perfect! I've got all the tweets we need. "
                        "I'll schedule them as requested."
                    )
                    
                    # Clear pending state
                    await self.db.update_session_metadata(session_id, {
                        "pending_tweets": None,
                        "approved_tweets": None,
                        "approval_state": "completed",
                        "last_action": "completed_approval"
                    })
                    
                    return {
                        "status": "completed",
                        "response": response
                    }
                
                # If we still need more tweets
                remaining = total_needed - len(approved_tweets)
                return {
                    "status": "in_progress",
                    "response": f"I've saved the approved tweets. We still need {remaining} more. Would you like me to generate them now?"
                }

            except json.JSONDecodeError:
                return {
                    "status": "error",
                    "response": "I didn't quite understand that. Could you please be more specific about which tweets you want to keep or regenerate?"
                }

        except Exception as e:
            logger.error(f"Error processing approval: {e}")
            return {
                "status": "error",
                "response": "I had trouble processing your response. Could you try again?"
            }