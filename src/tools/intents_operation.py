from datetime import datetime, UTC, timedelta
import logging
from typing import Dict, List, Optional, Any, Union
import json
from bson import ObjectId
import asyncio

from src.tools.base import (
    BaseTool,
    AgentResult,
    AgentDependencies,
    CommandAnalysis,
    ToolOperation,
    ToolRegistry
)
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService, ModelType
from src.db.mongo_manager import MongoManager
from src.db.enums import OperationStatus, ToolOperationState, ScheduleState, ContentType, ToolType
from src.utils.json_parser import parse_strict_json
from src.managers.approval_manager import ApprovalManager, ApprovalAction, ApprovalState
from src.managers.schedule_manager import ScheduleManager
from src.clients.coingecko_client import CoinGeckoClient
from src.clients.near_intents_client.intents_client import (
    intent_deposit, 
    smart_withdraw,
    intent_swap,
    get_intent_balance,
    wrap_near,
    IntentRequest,
    fetch_options,
    select_best_option,
    create_token_diff_quote
)
from src.clients.near_intents_client.config import (
    get_token_by_symbol,
    to_asset_id,
    to_decimals,
    from_decimals
)

logger = logging.getLogger(__name__)

class IntentsTool(BaseTool):
    """Limit order tool for NEAR protocol intents operations (deposit, swap, withdraw)"""
    
    # Static tool configuration
    name = "intents"
    description = "Perform limit order operations via NEAR intents (includes deposit, swap, withdraw)"
    version = "1.0"
    
    # Tool registry configuration - we'll need to add these enum values
    registry = ToolRegistry(
        content_type=ContentType.LIMIT_ORDER,
        tool_type=ToolType.INTENTS,
        requires_approval=True,
        requires_scheduling=True,
        required_clients=["coingecko_client", "near_account", "solver_bus_client"],  # Add solver_bus_client
        required_managers=["tool_state_manager", "approval_manager", "schedule_manager"]
    )

    def __init__(self, deps: Optional[AgentDependencies] = None):
        """Initialize intents tool with dependencies"""
        super().__init__()
        self.deps = deps or AgentDependencies()
        
        # Services will be injected by orchestrator based on registry requirements
        self.tool_state_manager = None
        self.llm_service = None
        self.approval_manager = None
        self.schedule_manager = None
        self.coingecko_client = None
        self.solver_bus_client = None
        self.near_account = None
        self.db = None
        
        # Add these lines for intent tracking
        self.intent_statuses = {}
        self.active_intents = {}

    def inject_dependencies(self, **services):
        """Inject required services - called by orchestrator during registration"""
        self.tool_state_manager = services.get("tool_state_manager")
        self.llm_service = services.get("llm_service")
        self.approval_manager = services.get("approval_manager")
        self.schedule_manager = services.get("schedule_manager")
        self.coingecko_client = services.get("coingecko_client")
        self.near_account = services.get("near_account")
        self.solver_bus_client = services.get("solver_bus_client")
        self.db = self.tool_state_manager.db if self.tool_state_manager else None

    async def run(self, input_data: str) -> Dict:
        """Run the intents tool - handles limit order flow"""
        try:
            # First check if this is an approval response
            if any(keyword in input_data.lower() for keyword in ["approve", "regenerate", "cancel"]):
                # Let approval manager handle the response
                return await self.approval_manager.process_approval_response(
                    message=input_data,
                    session_id=self.deps.session_id,
                    content_type=self.registry.content_type.value,
                    tool_operation_id=None
                )

            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            
            if not operation or operation.get('state') == ToolOperationState.COMPLETED.value:
                # Initial analysis and command flow for limit order
                command_info = await self._analyze_command(input_data)
                
                # Generate content for approval using count from command_info
                content_result = await self._generate_content(
                    topic=command_info["topic"],
                    count=command_info["item_count"],  # Use count from command analysis
                    schedule_id=command_info["schedule_id"],
                    tool_operation_id=command_info["tool_operation_id"]
                )
                
                # Start approval flow
                return await self.approval_manager.start_approval_flow(
                    session_id=self.deps.session_id,
                    tool_operation_id=command_info["tool_operation_id"],
                    items=content_result["items"]
                )
            else:
                # Let orchestrator handle ongoing operations
                # This includes monitoring service triggers and execution
                raise ValueError("Operation already in progress - should be handled by orchestrator")

        except Exception as e:
            logger.error(f"Error in intents tool: {e}", exc_info=True)
            return self.approval_manager.analyzer.create_error_response(str(e))

    async def _analyze_command(self, command: str) -> Dict:
        """Analyze command and setup initial monitoring for limit order"""
        try:
            logger.info(f"Starting command analysis for: {command}")
            
            # Get the existing operation that was created by orchestrator
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
                
            tool_operation_id = str(operation['_id'])
            
            # Update prompt to be more explicit about direction and price reference
            prompt = f"""You are a blockchain intents analyzer. Determine the limit order parameters for buying or selling a token based on the user's command.

Command: "{command}"

Required parameters for limit order:
   - item_count: number of limit orders to create (default 1)
   - topic: what to swap (e.g., "NEAR to USDC")
   - from_token: token being sold (e.g., "NEAR")
   - from_amount: amount of from_token to sell
   - to_token: token being bought (e.g., "USDC")
   - target_price_usd: target price in USD per reference_token
   - reference_token: token that the target price refers to
   - to_chain: destination chain for the to_token
   - expiration_hours: hours until order expires (optional, defaults to 24)
   - slippage: slippage tolerance percentage (optional, defaults to 0.5)

IMPORTANT RULES:
- When command says "at $X per TOKEN", TOKEN is the reference_token for pricing
- Direction matters: "swap A for B" means A is from_token and B is to_token
- If buying a token at $X per unit, that token is the reference_token
- Chain specification (e.g., "on Solana") refers to the to_chain

Example 1: "limit order swap 5 NEAR for USDC at $3.00 per NEAR"
Should parse as:
{{
    "item_count": 1,
    "topic": "swap 5 NEAR for USDC at $3.00 per NEAR",
    "from_token": "NEAR",
    "from_amount": 5.0,
    "to_token": "USDC",
    "target_price_usd": 3.0,
    "reference_token": "NEAR",
    "to_chain": "ethereum"
}}

Example 2: "limit order swap 1 USDC for NEAR at $2.20 per NEAR"
Should parse as:
{{
    "item_count": 1,
    "topic": "swap 1 USDC for NEAR at $2.20 per NEAR",
    "from_token": "USDC",
    "from_amount": 1.0,
    "to_token": "NEAR",
    "target_price_usd": 2.20,
    "reference_token": "NEAR",
    "to_chain": "near"
}}

Return ONLY valid JSON matching the example format.
"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a precise blockchain intents analyzer. Return ONLY valid JSON with no additional text."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            # Log the prompt being sent
            logger.info(f"Sending prompt to LLM: {messages}")

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.15,
                    "max_tokens": 500
                }
            )
            
            logger.info(f"Raw LLM response: {response}")
            
            try:
                # Parse response and use it directly as parameters
                params = json.loads(response)
                logger.info(f"Parsed JSON data: {params}")
                
                # Validate required fields
                required_fields = ['from_token', 'from_amount', 'to_token', 'target_price_usd', 'reference_token']
                missing_fields = [field for field in required_fields if field not in params]
                if missing_fields:
                    raise ValueError(f"Missing required fields: {missing_fields}")
                
                # Set up monitoring parameters
                monitoring_params = {
                    "check_interval_seconds": 60,
                    "last_checked_timestamp": int(datetime.now(UTC).timestamp()),
                    "best_price_seen": 0,
                    "expiration_timestamp": int((datetime.now(UTC) + timedelta(hours=params.get("expiration_hours", 24))).timestamp()),
                    "max_checks": 1000,
                    "reference_token": params["reference_token"]  # Add reference token to monitoring params
                }
                
                # Create topic string using reference token for price
                topic = f"Limit order: {params['from_token']} to {params['to_token']} at ${params['target_price_usd']} per {params['reference_token']}"
                
                # Create schedule FIRST
                schedule_id = await self.schedule_manager.initialize_schedule(
                    tool_operation_id=tool_operation_id,
                    schedule_info={
                        "schedule_type": "monitoring",
                        "operation_type": "limit_order",
                        "total_items": params["item_count"],
                        "monitoring_params": monitoring_params
                    },
                    content_type=self.registry.content_type.value,
                    session_id=self.deps.session_id
                )
                
                # THEN update operation with the schedule_id
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=tool_operation_id,
                    input_data={
                        "command_info": {
                            "operation_type": "limit_order",
                            "parameters": params,
                            "monitoring_params": monitoring_params,
                            "topic": topic,
                            "item_count": params["item_count"]
                        },
                        "schedule_id": schedule_id
                    },
                    metadata={
                        "schedule_state": ScheduleState.PENDING.value,
                        "schedule_id": schedule_id,
                        "operation_type": "limit_order"
                    }
                )
                
                # FINALLY return all required information
                return {
                    "tool_operation_id": tool_operation_id,
                    "topic": topic,
                    "item_count": params["item_count"],
                    "schedule_id": schedule_id,
                    
                    # Required by approval_manager
                    "tool_registry": {
                        "requires_approval": True,
                        "requires_scheduling": True,
                        "content_type": self.registry.content_type.value,
                        "tool_type": self.registry.tool_type.value
                    },
                    
                    # Required by schedule_manager
                    "schedule_info": {
                        "schedule_type": "monitoring",
                        "operation_type": "limit_order",
                        "total_items": params["item_count"],
                        "monitoring_params": monitoring_params
                    },
                    
                    # Limit order specific parameters
                    "parameters": {
                        "price_oracle": {
                            "symbol": params["reference_token"],
                            "target_price_usd": params["target_price_usd"]
                        },
                        "swap": {
                            "from_token": params["from_token"],
                            "from_amount": params["from_amount"],
                            "to_token": params["to_token"],
                            "chain_out": params.get("to_chain", "ethereum")
                        },
                        "withdraw": {
                            "enabled": bool(params.get("destination_address")),
                            "destination_address": params.get("destination_address"),
                            "destination_chain": params.get("destination_chain", "ethereum")
                        }
                    }
                }

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse LLM response as JSON: {e}")
                logger.error(f"Raw response that failed parsing: {response}")
                raise
            except Exception as e:
                logger.error(f"Error processing LLM response: {e}")
                raise

        except Exception as e:
            logger.error(f"Error in limit order analysis: {e}", exc_info=True)
            raise

    async def _generate_content(
        self, 
        topic: str, 
        count: int, 
        schedule_id: str = None, 
        tool_operation_id: str = None
    ) -> Dict:
        """Generate human-readable content for limit order approval"""
        try:
            logger.info(f"Generating {count} limit order(s) for topic: {topic}")
            
            # Get parent operation to access stored parameters
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
            
            # Get the parameters from _analyze_command
            command_info = operation.get("input_data", {}).get("command_info", {})
            params = command_info.get("parameters", {})
            logger.info(f"Using parameters for content generation: {params}")
            
            # Track all generated items
            saved_items = []
            current_pending_items = operation.get("output_data", {}).get("pending_items", [])

            # Generate multiple items based on count
            for i in range(count):
                # Generate description using LLM with improved prompt
                prompt = f"""You are a cryptocurrency expert. Generate a detailed description for a limit order with the following parameters:

Operation Details:
- Swap {params['from_amount']} {params['from_token']} for {params['to_token']}
- Target Price: ${params['target_price_usd']} per {params['from_token']}
- Output Chain: {params.get('to_chain', 'ethereum')}
- Destination: {params.get('destination_address', 'default wallet')} on {params.get('destination_chain', 'ethereum')}
- Expires in: {params.get('expiration_hours', 24)} hours

Include:
1. A clear title summarizing the limit order
2. A detailed description of what will happen when executed
3. Expected outcome when price target is met

IMPORTANT: Your response MUST be valid JSON in the following format:
{{
    "title": "Limit Order Summary",
    "description": "Detailed description here...",
    "expected_outcome": "Expected outcome description"
}}

Do not include any text outside of this JSON structure."""

                messages = [
                    {
                        "role": "system",
                        "content": "You are a cryptocurrency expert. Generate clear, detailed descriptions for limit orders. Return ONLY valid JSON with no markdown formatting or additional text."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]

                # Log the prompt being sent
                logger.info(f"Sending content generation prompt to LLM")

                # Get LLM response with increased max_tokens and lower temperature
                response = await self.llm_service.get_response(
                    prompt=messages,
                    model_type=ModelType.GROQ_LLAMA_3_3_70B,
                    override_config={
                        "temperature": 0.15,  # Lower temperature for more predictable output
                        "max_tokens": 800    # Increased token limit
                    }
                )
                
                # Log the raw response for debugging
                logger.info(f"Raw LLM response for content generation: {response}")
                
                # Clean up response - remove any markdown formatting
                cleaned_response = response.strip()
                if cleaned_response.startswith('```') and cleaned_response.endswith('```'):
                    # Remove markdown code blocks
                    cleaned_response = '\n'.join(cleaned_response.split('\n')[1:-1])
                
                # Remove any non-JSON text before or after the JSON structure
                if '{' in cleaned_response and '}' in cleaned_response:
                    start_idx = cleaned_response.find('{')
                    end_idx = cleaned_response.rfind('}') + 1
                    cleaned_response = cleaned_response[start_idx:end_idx]
                
                logger.info(f"Cleaned response: {cleaned_response}")
                
                # Try to parse the response as JSON with robust error handling
                try:
                    generated_content = json.loads(cleaned_response)
                    logger.info(f"Successfully parsed JSON content: {generated_content}")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse LLM response as JSON: {e}")
                    
                    # Try to use the parse_strict_json utility if available
                    try:
                        generated_content = parse_strict_json(cleaned_response)
                        logger.info(f"Successfully parsed with parse_strict_json: {generated_content}")
                    except Exception as parse_error:
                        logger.error(f"Failed to parse with parse_strict_json: {parse_error}")
                        
                        # Last resort: create minimal content structure
                        logger.warning("Using minimal content structure as last resort")
                        generated_content = {
                            "title": f"Limit Order: {params['from_token']} to {params['to_token']} at ${params['target_price_usd']}",
                            "description": f"This limit order will execute when {params['from_token']} reaches ${params['target_price_usd']}.",
                            "expected_outcome": f"Exchange {params['from_amount']} {params['from_token']} for {params['to_token']}."
                        }
                
                # Create tool item for approval
                tool_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": tool_operation_id,
                    "schedule_id": schedule_id,
                    "content_type": self.registry.content_type.value,
                    "state": operation["state"],
                    "status": OperationStatus.PENDING.value,  # Individual item status
                    "content": {
                        "title": generated_content.get("title", f"Limit Order: {params['from_token']} to {params['to_token']}"),
                        "description": generated_content.get("description", ""),
                        "expected_outcome": generated_content.get("expected_outcome", ""),
                        "operation_details": {
                            "from_token": params["from_token"],
                            "from_amount": params["from_amount"],
                            "to_token": params["to_token"],
                            "target_price_usd": params["target_price_usd"],
                            "reference_token": params["reference_token"],
                            "to_chain": params.get("to_chain", "ethereum"),
                            "destination_address": params.get("destination_address"),
                            "destination_chain": params.get("destination_chain", "ethereum"),
                            "expiration_hours": params.get("expiration_hours", 24)
                        }
                    },
                    "metadata": {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "scheduling_type": "monitored",
                        "order_index": i + 1,  # Add order index
                        "total_orders": count,  # Add total count
                        "state_history": [{
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value,
                            "timestamp": datetime.now(UTC).isoformat()
                        }]
                    }
                }
                
                # Save item
                result = await self.db.tool_items.insert_one(tool_item)
                item_id = str(result.inserted_id)
                tool_item["_id"] = item_id
                
                # Add to pending items list
                current_pending_items.append(item_id)
                saved_items.append(tool_item)
                
                logger.info(f"Created limit order item {i+1}/{count} with ID {item_id}")

            # Update operation with all pending items
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,
                content_updates={
                    "pending_items": current_pending_items
                },
                metadata={
                    "item_states": {
                        str(item["_id"]): {
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value
                        }
                        for item in saved_items
                    },
                    "total_items": count,
                    "generated_items": len(saved_items)
                }
            )

            logger.info(f"Successfully generated {len(saved_items)} limit order items")
            return {
                "items": saved_items,
                "schedule_id": schedule_id,
                "tool_operation_id": tool_operation_id
            }

        except Exception as e:
            logger.error(f"Error generating limit order content: {e}", exc_info=True)
            raise

    async def execute_scheduled_operation(self, operation: Dict) -> Dict:
        """Execute a scheduled limit order operation following the intents lifecycle"""
        try:
            logger.info(f"Executing limit order operation: {operation.get('_id')}")
            
            # Extract operation parameters from the correct location
            content = operation.get("content", {})
            operation_details = content.get("operation_details", {})
            
            # Create new variables to ensure correct types
            from_token = str(operation_details.get("from_token", ""))
            from_amount = float(operation_details.get("from_amount", 0))
            to_token = str(operation_details.get("to_token", ""))
            chain_out = str(operation_details.get("to_chain", "ethereum"))
            
            logger.info(f"Executing swap with parameters: from_token='{from_token}', "
                       f"from_amount={from_amount}, to_token='{to_token}', chain_out='{chain_out}'")
            
            # Validate required parameters
            if not from_token or from_amount <= 0 or not to_token:
                raise ValueError(f"Missing or invalid parameters: from_token={from_token}, from_amount={from_amount}, to_token={to_token}")

            execution_steps = []
            try:
                # IMPORTANT: Remove 'await' - this is not an async function
                logger.info(f"Checking balance for token: '{from_token}'")
                initial_balance = get_intent_balance(self.near_account, from_token)
                initial_balance_float = float(initial_balance) if initial_balance is not None else 0
                
                logger.info(f"Initial {from_token} balance in intents: {initial_balance_float}")
                execution_steps.append({
                    "step": "check_balance",
                    "result": {"initial_balance": initial_balance_float}
                })

                # Handle deposit if needed
                if initial_balance_float < from_amount:
                    needed_amount = from_amount - initial_balance_float
                    logger.info(f"Depositing {needed_amount} {from_token}")
                    
                    if from_token == "NEAR":
                        # IMPORTANT: Remove 'await' here too
                        wrap_result = wrap_near(self.near_account, needed_amount)
                        logger.info(f"Wrapped NEAR result: {wrap_result}")
                        execution_steps.append({
                            "step": "wrap_near",
                            "result": wrap_result
                        })
                        await asyncio.sleep(15)  # Keep this await - asyncio.sleep is async
                    
                    # IMPORTANT: Remove 'await' here too
                    deposit_result = intent_deposit(self.near_account, from_token, needed_amount)
                    logger.info(f"Deposit result: {deposit_result}")
                    execution_steps.append({
                        "step": "deposit",
                        "result": deposit_result
                    })
                    await asyncio.sleep(15)  # Keep this await
                    
                    # IMPORTANT: Remove 'await' here too
                    new_balance = get_intent_balance(self.near_account, from_token)
                    new_balance_float = float(new_balance) if new_balance is not None else 0
                    if new_balance_float < from_amount:
                        raise ValueError(f"Deposit verification failed. Balance: {new_balance_float} {from_token}")

                # Execute swap - remove 'await' here too
                logger.info(f"Executing swap: {from_amount} {from_token} -> {to_token}")
                swap_result = intent_swap(
                    self.near_account,
                    from_token,
                    from_amount,
                    to_token,
                    chain_out=chain_out
                )
                
                if not swap_result or 'error' in swap_result:
                    raise Exception(f"Swap failed: {swap_result.get('error', 'Unknown error')}")
                
                execution_steps.append({
                    "step": "swap",
                    "result": swap_result
                })
                
                # Wait for swap to complete
                await asyncio.sleep(15)
                
                # Calculate received amount using from_decimals
                received_amount = from_decimals(swap_result.get('amount_out', 0), to_token)
                logger.info(f"Swap successful. Received {received_amount} {to_token}")

                # 4. Handle withdrawal if enabled
                if operation_details.get("destination_address"):
                    logger.info(f"Withdrawing {received_amount} {to_token} to {operation_details['destination_address']} on {operation_details['destination_chain']}")
                    
                    withdrawal_result = smart_withdraw(
                        account=self.near_account,
                        token=to_token,
                        amount=received_amount,
                        destination_address=operation_details['destination_address'],
                        destination_chain=operation_details['destination_chain']
                    )
                    
                    if not withdrawal_result or 'error' in withdrawal_result:
                        raise Exception(f"Withdrawal failed: {withdrawal_result.get('error', 'Unknown error')}")
                    
                    execution_steps.append({
                        "step": "withdraw",
                        "result": withdrawal_result
                    })
                    
                    logger.info(f"Withdrawal successful: {withdrawal_result}")
                    
                    # Wait for withdrawal to complete
                    await asyncio.sleep(15)

                # 5. Final balance check
                final_balance = get_intent_balance(self.near_account, to_token)
                execution_steps.append({
                    "step": "final_balance",
                    "result": {"final_balance": final_balance}
                })

                return {
                    'success': True,
                    'execution_steps': execution_steps,
                    'final_result': {
                        'from_token': from_token,
                        'from_amount': from_amount,
                        'to_token': to_token,
                        'received_amount': received_amount,
                        'destination_chain': operation_details.get('destination_chain', chain_out),
                        'withdrawal_executed': bool(operation_details.get('destination_address'))
                    },
                    'execution_time': datetime.now(UTC).isoformat()
                }

            except Exception as e:
                logger.error(f"Error in execution steps: {e}", exc_info=True)
                return {
                    'success': False,
                    'error': str(e),
                    'execution_steps': execution_steps,  # Include steps completed before error
                    'execution_time': datetime.now(UTC).isoformat()
                }

        except Exception as e:
            logger.error(f"Error in execute_scheduled_operation: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

    def can_handle(self, command_text: str, tool_type: Optional[str] = None) -> bool:
        """Check if this tool can handle the given command
        
        This method relies on the tool_type passed from the trigger detector
        rather than duplicating keyword detection logic.
        """
        # If tool_type is explicitly specified as 'intents', handle it
        if tool_type and tool_type.lower() == self.registry.tool_type.value.lower():
            logger.info(f"IntentsTool handling command based on explicit tool_type: {tool_type}")
            return True
        
        # Otherwise, don't try to detect keywords here - that's the trigger detector's job
        return False