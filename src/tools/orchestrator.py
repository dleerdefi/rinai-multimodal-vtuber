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
            priority_groups = {}
            for tool in tools:
                priority_groups.setdefault(tool.priority, []).append(tool)
            
            results = {}
            for priority in sorted(priority_groups.keys()):
                group = priority_groups[priority]
                tasks = []
                
                for tool in group:
                    logger.debug(f"Processing tool: {tool.tool_name}, action: {tool.action}")
                    
                    if tool.tool_name == "perplexity_search":
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