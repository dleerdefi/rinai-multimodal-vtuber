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
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            
            # Check if we need to start a new operation
            if not operation or operation.get('state') in [ToolOperationState.COMPLETED.value, ToolOperationState.ERROR.value]:
                # Initial analysis and command flow for limit order
                command_info = await self._analyze_command(input_data)

                # Generate content for approval using count from command_info
                content_result = await self._generate_content(
                    topic=command_info["topic"],
                    count=command_info["item_count"],
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
                raise ValueError("Operation already in progress - should be handled by orchestrator")

        except Exception as e:
            logger.error(f"Error in intents tool: {e}", exc_info=True)
            return self.approval_manager.analyzer.create_error_response(str(e))

    async def _analyze_command(
        self,
        command: str,
        is_regeneration: bool = False
    ) -> Dict:
        """Analyze command and store analysis results"""
        try:
            logger.info(f"Starting command analysis for: {command}")
            
            # Get operation to retrieve session_id if not provided
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
                
            tool_operation_id = str(operation['_id'])
            
            # Update prompt to be more strict
            prompt = f"""You are a blockchain intents analyzer. Parse the following command into limit order parameters.

Command: "{command}"

Return a JSON object with EXACTLY these fields:
{{
    "item_count": number of orders (default 1),
    "topic": "descriptive text of the order",
    "from_token": "token being sold",
    "from_amount": numeric amount to sell,
    "to_token": "token being bought",
    "target_price_usd": numeric target price in USD,
    "reference_token": "token the price refers to",
    "to_chain": "destination chain",
    "destination_address": "withdrawal address (if specified)",
    "destination_chain": "withdrawal chain (if different from to_chain)"
}}

Rules:
1. ALL numeric values must be numbers (not strings)
2. ALL chain names must be lowercase
3. If withdrawal is specified, BOTH destination_address and destination_chain are required
4. Chain names must be one of: "ethereum", "solana", "arbitrum", "near", "base"

Example command: "limit order swap 0.15 NEAR for SOL and withdraw to Solana address abc123 when NEAR reaches $2.50"
Example response:
{{
    "item_count": 1,
    "topic": "swap 0.15 NEAR for SOL with withdrawal to Solana",
    "from_token": "NEAR",
    "from_amount": 0.15,
    "to_token": "SOL",
    "target_price_usd": 2.50,
    "reference_token": "NEAR",
    "to_chain": "solana",
    "destination_address": "abc123",
    "destination_chain": "solana"
}}

Parse the command and return ONLY the JSON object with no additional text."""

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
                # First try parse_strict_json which handles markdown
                parsed_data = parse_strict_json(response)
                if not parsed_data:
                    # If that fails, try cleaning the response
                    cleaned_response = response.strip()
                    if '```' in cleaned_response:
                        # Get content between backticks
                        parts = cleaned_response.split('```')
                        for part in parts:
                            if '{' in part and '}' in part:
                                cleaned_response = part.strip()
                                if cleaned_response.startswith('json'):
                                    cleaned_response = cleaned_response[4:].strip()
                                try:
                                    parsed_data = json.loads(cleaned_response)
                                    break
                                except json.JSONDecodeError:
                                    continue
                    if not parsed_data:
                        raise ValueError("Failed to parse LLM response into valid JSON")

                # Handle both single order and array formats
                if isinstance(parsed_data, dict):
                    if "limit_orders" in parsed_data:
                        orders = parsed_data["limit_orders"]
                    else:
                        orders = [parsed_data]
                else:
                    orders = parsed_data if isinstance(parsed_data, list) else [parsed_data]
                
                # Validate each order using config-based validation
                orders = [self._validate_order(order) for order in orders]
                logger.info(f"Successfully parsed and validated orders: {orders}")
                
                # Use first order's item_count since they should all be the same
                item_count = orders[0]["item_count"]
                
                # Set up monitoring parameters for each order
                monitoring_params_list = []
                topics = []
                
                for order in orders:
                    monitoring_params = {
                        "check_interval_seconds": 60,
                        "last_checked_timestamp": int(datetime.now(UTC).timestamp()),
                        "best_price_seen": 0,
                        "expiration_timestamp": int((datetime.now(UTC) + timedelta(hours=order.get("expiration_hours", 24))).timestamp()),
                        "max_checks": 1000,
                        "reference_token": order["reference_token"],
                        "target_price_usd": float(order["target_price_usd"]),
                        "from_token": order["from_token"],
                        "from_amount": float(order["from_amount"]),
                        "to_token": order["to_token"],
                        "to_chain": order["to_chain"],
                        "destination_chain": order.get("destination_chain", order["to_chain"]),
                        "destination_address": order.get("destination_address"),
                        "from_asset_id": order.get("from_asset_id"),
                        "to_asset_id": order.get("to_asset_id")
                    }
                    monitoring_params_list.append(monitoring_params)
                    
                    # Create topic string using reference token for price
                    topic = f"Limit order: {order['from_token']} to {order['to_token']} at ${order['target_price_usd']} per {order['reference_token']}"
                    topics.append(topic)
                
                # After parsing orders, check if this is regeneration
                if is_regeneration:
                    # Get the collecting item that needs regeneration
                    collecting_items = await self.tool_state_manager.get_operation_items(
                        tool_operation_id=tool_operation_id,
                        state=ToolOperationState.COLLECTING.value,
                        status=OperationStatus.PENDING.value
                    )
                    
                    if not collecting_items:
                        raise ValueError("No regeneration items found in COLLECTING state")

                    # Store analysis results in the correct item
                    for item in collecting_items:
                        await self.db.store_tool_item_content(
                            item_id=str(item['_id']),  # Use item ID not operation ID
                            content={
                                "operation_type": "limit_order",
                                "operation_details": orders[0]  # Store initial details
                            },
                            operation_details=orders[0],
                            source='analyze_command_regeneration',
                            tool_operation_id=tool_operation_id
                        )

                    # Update operation with analyzed parameters
                    await self.tool_state_manager.update_operation(
                        session_id=self.deps.session_id,
                        tool_operation_id=tool_operation_id,
                        input_data={
                            "regeneration_analysis": {
                                "orders": orders,
                                "analyzed_at": datetime.now(UTC).isoformat()
                            }
                        }
                    )
                    
                    logger.info("Skipping schedule creation for regeneration analysis")
                    return {
                        "orders": orders,
                        "monitoring_params_list": monitoring_params_list,  # Return in same format as initial turn
                        "topics": topics
                    }
                
                # Create schedule FIRST with all necessary info
                schedule_id = await self.schedule_manager.initialize_schedule(
                    tool_operation_id=tool_operation_id,
                    schedule_info={
                        "schedule_type": "monitoring",
                        "operation_type": "limit_order",
                        "total_items": len(orders),
                        "monitoring_params_list": monitoring_params_list,
                        "topics": topics,
                        "content_type": self.registry.content_type.value,
                        "tool_type": self.registry.tool_type.value,
                        "requires_approval": True,
                        "requires_scheduling": True
                    },
                    content_type=self.registry.content_type.value,
                    session_id=self.deps.session_id
                )
                
                # THEN update operation with the schedule_id and all necessary info
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=tool_operation_id,
                    input_data={
                        "command_info": {
                            "operation_type": "limit_order",
                            "orders": orders,
                            "monitoring_params_list": monitoring_params_list,
                            "topics": topics,
                            "item_count": len(orders)
                        },
                        "schedule_id": schedule_id
                    },
                    metadata={
                        "schedule_state": ScheduleState.PENDING.value,
                        "schedule_id": schedule_id,
                        "operation_type": "limit_order",
                        "content_type": self.registry.content_type.value,
                        "tool_type": self.registry.tool_type.value,
                        "requires_approval": True,
                        "requires_scheduling": True
                    }
                )
                
                # FINALLY return all required information
                return {
                    "tool_operation_id": tool_operation_id,
                    "topics": topics,
                    "item_count": len(orders),
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
                        "total_items": len(orders),
                        "monitoring_params_list": monitoring_params_list
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
        topic: Optional[str] = None,
        count: int = 1, 
        revision_instructions: str = None,
        schedule_id: Optional[str] = None, 
        tool_operation_id: str = None,
        analyzed_params: Optional[Dict] = None
    ) -> Dict:
        """Generate content and store it properly"""
        try:
            logger.info(f"Generating {count} limit order(s)")
            if revision_instructions:
                logger.info(f"With revision instructions: {revision_instructions}")
            
            # Get parent operation to access stored parameters
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
            
            # Get the schedule_id from operation if not provided
            operation_schedule_id = (
                operation.get('metadata', {}).get('schedule_id') or 
                operation.get('input_data', {}).get('schedule_id')
            )
            if operation_schedule_id:
                schedule_id = operation_schedule_id
                logger.info(f"Using schedule_id from operation: {schedule_id}")
            
            if not schedule_id:
                raise ValueError("No schedule_id found in operation or parameters")
            
            # Get the orders and monitoring params from command_info
            command_info = operation.get("input_data", {}).get("command_info", {})
            orders = command_info.get("orders", [])
            monitoring_params_list = command_info.get("monitoring_params_list", [])
            
            if not orders:
                raise ValueError("No orders found in operation command_info")
            
            # Check if we're regenerating content
            is_regenerating = operation.get("metadata", {}).get("approval_state") == ApprovalState.REGENERATING.value
            logger.info(f"Generating content in {'regeneration' if is_regenerating else 'initial'} mode")
            
            # IMPORTANT: If regenerating, only process the specified number of items
            if is_regenerating:
                # Find existing COLLECTING items created by approval_manager
                collecting_items = await self.tool_state_manager.get_operation_items(
                    tool_operation_id=tool_operation_id,
                    state=ToolOperationState.COLLECTING.value,
                    status=OperationStatus.PENDING.value
                )
                
                if not collecting_items:
                    raise ValueError("No regeneration items found in COLLECTING state")
                
                logger.info(f"Found {len(collecting_items)} existing items to update")

                # Get the stored analysis results
                stored_analysis = operation.get('input_data', {}).get('regeneration_analysis', {})
                
                # Use stored analysis with fallback to provided params
                order = analyzed_params or stored_analysis.get('orders', [{}])[0]
                
                # Create monitoring params from stored analysis
                monitoring_params = {
                    "check_interval_seconds": 60,
                    "last_checked_timestamp": int(datetime.now(UTC).timestamp()),
                    "best_price_seen": 0,
                    "expiration_timestamp": int((datetime.now(UTC) + timedelta(hours=24)).timestamp()),
                    "max_checks": 1000,
                    "reference_token": order["reference_token"],
                    "target_price_usd": float(order["target_price_usd"]),
                    "from_token": order["from_token"],
                    "from_amount": float(order["from_amount"]),
                    "to_token": order["to_token"],
                    "to_chain": order["to_chain"],
                    "destination_chain": order.get("destination_chain", order["to_chain"]),
                    "destination_address": order.get("destination_address"),
                    "from_asset_id": order.get("from_asset_id"),
                    "to_asset_id": order.get("to_asset_id")
                }

                saved_items = []
                for item in collecting_items:
                    # Use existing prompt logic to generate content
                    base_prompt = f"""You are a cryptocurrency expert. Generate a detailed description for a limit order with the following parameters:

Operation Details:
- Swap {order['from_amount']} {order['from_token']} for {order['to_token']}
- Target Price: ${order['target_price_usd']} per {order['reference_token']}
- Output Chain: {order.get('to_chain', 'ethereum')}
- Destination: {order.get('destination_address', 'default wallet')} on {order.get('destination_chain', 'ethereum')}
- Expires in: {order.get('expiration_hours', 24)} hours"""

                    # Add revision instructions if provided
                    if revision_instructions:
                        base_prompt += f"\n\nImportant revision instructions: {revision_instructions}"

                    base_prompt += """

Include:
1. A clear title summarizing the limit order
2. A detailed description of what will happen when executed
3. Expected outcome when price target is met

IMPORTANT: Your response MUST be valid JSON in the following format:
{
    "title": "Limit Order Summary",
    "description": "Detailed description here...",
    "expected_outcome": "Expected outcome description"
}"""

                    messages = [
                        {
                            "role": "system",
                            "content": "You are a cryptocurrency expert. Generate clear, detailed descriptions for limit orders. Return ONLY valid JSON."
                        },
                        {
                            "role": "user",
                            "content": base_prompt
                        }
                    ]

                    # Get LLM response
                    response = await self.llm_service.get_response(
                        prompt=messages,
                        model_type=ModelType.GROQ_LLAMA_3_3_70B,
                        override_config={
                            "temperature": 0.15,
                            "max_tokens": 800
                        }
                    )
                    
                    try:
                        generated_content = json.loads(response)
                    except json.JSONDecodeError:
                        try:
                            generated_content = parse_strict_json(response)
                        except Exception:
                            generated_content = {
                                "title": f"Limit Order: {order['from_token']} to {order['to_token']} at ${order['target_price_usd']}",
                                "description": f"This limit order will execute when {order['reference_token']} reaches ${order['target_price_usd']}.",
                                "expected_outcome": f"Exchange {order['from_amount']} {order['from_token']} for {order['to_token']}."
                            }
                    
                    # First store content without parameters
                    await self.db.store_tool_item_content(
                        item_id=str(item['_id']),
                        content={
                            "title": generated_content.get("title"),
                            "description": generated_content.get("description"),
                            "expected_outcome": generated_content.get("expected_outcome"),
                            "operation_type": "limit_order",
                            "operation_details": {
                                "from_token": order["from_token"],
                                "from_amount": str(order["from_amount"]),
                                "to_token": order["to_token"],
                                "target_price_usd": float(order["target_price_usd"]),
                                "reference_token": order["reference_token"],
                                "to_chain": order["to_chain"],
                                "from_asset_id": order.get("from_asset_id"),
                                "to_asset_id": order.get("to_asset_id"),
                                "destination_address": order.get("destination_address"),
                                "destination_chain": order.get("destination_chain")
                            }
                        },
                        operation_details=order,
                        source='generate_content',
                        tool_operation_id=tool_operation_id
                    )

                    # Then update the item with monitoring params separately
                    await self.db.tool_items.find_one_and_update(
                        {"_id": item["_id"]},
                        {"$set": {
                            "parameters": {"custom_params": monitoring_params},
                            "metadata": {
                                "content_updated_at": datetime.now(UTC).isoformat(),
                                "regeneration_info": {
                                    "is_regenerated": True,
                                    "revision_instructions": revision_instructions,
                                    "regenerated_at": datetime.now(UTC).isoformat(),
                                    "original_order": dict(order),
                                    "original_schedule_id": operation.get('metadata', {}).get('schedule_id')
                                }
                            }
                        }},
                        return_document=True
                    )

                    saved_items.append(item)
                    logger.info(f"Updated content and parameters for regenerated item {item['_id']}")

                # Update operation with regenerated items
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=tool_operation_id,
                    content_updates={
                        "items": saved_items
                    },
                    metadata={
                        "regeneration_info": {
                            "is_regenerated": True,
                            "regenerated_at": datetime.now(UTC).isoformat(),
                            "regenerated_items": [str(item["_id"]) for item in saved_items],
                            "original_schedule_id": operation.get('metadata', {}).get('schedule_id')
                        }
                    }
                )

                return {
                    "items": saved_items,
                    "schedule_id": operation.get('metadata', {}).get('schedule_id'),
                    "tool_operation_id": tool_operation_id,
                    "regeneration_needed": False
                }
            
            # Track all generated items
            saved_items = []
            current_pending_items = operation.get("output_data", {}).get("pending_items", [])

            # Generate content for each order
            for i, order in enumerate(orders):
                # Base prompt for limit order description
                base_prompt = f"""You are a cryptocurrency expert. Generate a detailed description for a limit order with the following parameters:

Operation Details:
- Swap {order['from_amount']} {order['from_token']} for {order['to_token']}
- Target Price: ${order['target_price_usd']} per {order['reference_token']}
- Output Chain: {order.get('to_chain', 'ethereum')}
- Destination: {order.get('destination_address', 'default wallet')} on {order.get('destination_chain', 'ethereum')}
- Expires in: {order.get('expiration_hours', 24)} hours"""

                # Add revision instructions if provided
                if revision_instructions:
                    base_prompt += f"\n\nImportant revision instructions: {revision_instructions}"

                base_prompt += """

Include:
1. A clear title summarizing the limit order
2. A detailed description of what will happen when executed
3. Expected outcome when price target is met

IMPORTANT: Your response MUST be valid JSON in the following format:
{
    "title": "Limit Order Summary",
    "description": "Detailed description here...",
    "expected_outcome": "Expected outcome description"
}"""

                messages = [
                    {
                        "role": "system",
                        "content": "You are a cryptocurrency expert. Generate clear, detailed descriptions for limit orders. Return ONLY valid JSON."
                    },
                    {
                        "role": "user",
                        "content": base_prompt
                    }
                ]

                # Get LLM response
                response = await self.llm_service.get_response(
                    prompt=messages,
                    model_type=ModelType.GROQ_LLAMA_3_3_70B,
                    override_config={
                        "temperature": 0.15,
                        "max_tokens": 800
                    }
                )
                
                try:
                    generated_content = json.loads(response)
                except json.JSONDecodeError:
                    try:
                        generated_content = parse_strict_json(response)
                    except Exception:
                        generated_content = {
                            "title": f"Limit Order: {order['from_token']} to {order['to_token']} at ${order['target_price_usd']}",
                            "description": f"This limit order will execute when {order['reference_token']} reaches ${order['target_price_usd']}.",
                            "expected_outcome": f"Exchange {order['from_amount']} {order['from_token']} for {order['to_token']}."
                        }
                
                # Create tool item with monitoring parameters
                tool_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": tool_operation_id,
                    "schedule_id": schedule_id,
                    "content_type": self.registry.content_type.value,
                    "state": operation["state"],
                    "status": OperationStatus.PENDING.value,
                    "content": {
                        "title": generated_content.get("title"),
                        "description": generated_content.get("description"),
                        "expected_outcome": generated_content.get("expected_outcome"),
                        "operation_type": "limit_order",
                        "operation_details": {
                            "from_token": order["from_token"],
                            "from_amount": str(order["from_amount"]),
                            "to_token": order["to_token"],
                            "target_price_usd": float(order["target_price_usd"]),
                            "reference_token": order["reference_token"],
                            "to_chain": order["to_chain"],
                            "from_asset_id": order.get("from_asset_id"),
                            "to_asset_id": order.get("to_asset_id"),
                            "destination_address": order.get("destination_address"),
                            "destination_chain": order.get("destination_chain")
                        }
                    },
                    "parameters": {
                        "custom_params": monitoring_params_list[min(i, len(monitoring_params_list)-1)]  # Use appropriate monitoring params, with fallback
                    },
                    "metadata": {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "scheduling_type": "monitored",
                        "monitoring_started_at": datetime.now(UTC).isoformat(),
                        "monitoring_expiration": datetime.fromtimestamp(
                            monitoring_params_list[min(i, len(monitoring_params_list)-1)]["expiration_timestamp"],
                            UTC
                        ).isoformat(),
                        "order_index": i + 1,
                        "total_orders": len(orders),
                        "state_history": [{
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value,
                            "timestamp": datetime.now(UTC).isoformat()
                        }],
                        "regeneration_info": {
                            "is_regenerated": is_regenerating,
                            "revision_instructions": revision_instructions,
                            "regenerated_at": datetime.now(UTC).isoformat() if is_regenerating else None,
                            "original_order": dict(order) if is_regenerating else None
                        } if is_regenerating else None
                    }
                }
                
                # Save item
                result = await self.db.tool_items.insert_one(tool_item)
                item_id = str(result.inserted_id)
                tool_item["_id"] = item_id
                
                # Add to pending items list
                current_pending_items.append(item_id)
                saved_items.append(tool_item)
                
                logger.info(f"Created limit order item {i+1}/{len(orders)} with ID {item_id}")
                logger.info(f"Item content: {tool_item['content']}")
                logger.info(f"Item monitoring params: {tool_item['parameters']['custom_params']}")

            # Update operation with all pending items
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,
                content_updates={
                    "pending_items": current_pending_items,
                    "items": saved_items  # Add this to ensure all items are tracked
                },
                metadata={
                    "item_states": {
                        str(item["_id"]): {
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value
                        }
                        for item in saved_items
                    },
                    "total_items": len(orders),
                    "generated_items": len(saved_items),
                    "schedule_id": schedule_id,
                    "regeneration_info": {
                        "is_regenerated": is_regenerating,
                        "revision_instructions": revision_instructions,
                        "regenerated_at": datetime.now(UTC).isoformat()
                    } if is_regenerating else None
                }
            )

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
            custom_params = operation.get("parameters", {}).get("custom_params", {})
            
            # Use chain info from custom_params first, fall back to operation_details
            chain_out = str(
                custom_params.get("destination_chain") or 
                custom_params.get("to_chain") or 
                operation_details.get("destination_chain") or 
                operation_details.get("to_chain", "ethereum")
            )
            
            # Create new variables to ensure correct types
            from_token = str(operation_details.get("from_token", ""))
            from_amount = float(operation_details.get("from_amount", 0))
            to_token = str(operation_details.get("to_token", ""))
            
            logger.info(f"Executing swap with parameters: from_token='{from_token}', "
                       f"from_amount={from_amount}, to_token='{to_token}', chain_out='{chain_out}'")
            
            # Validate required parameters
            if not from_token or from_amount <= 0 or not to_token:
                raise ValueError(f"Missing or invalid parameters: from_token={from_token}, from_amount={from_amount}, to_token={to_token}")

            execution_steps = []
            try:
                # Step 1: Check Balance
                initial_balance = get_intent_balance(self.near_account, from_token)
                initial_balance_float = float(initial_balance) if initial_balance is not None else 0
                execution_steps.append({
                    "step": "check_balance",
                    "result": {"initial_balance": initial_balance_float},
                    "success": True
                })

                # Step 2: Handle deposit if needed
                if initial_balance_float < from_amount:
                    needed_amount = from_amount - initial_balance_float
                    
                    if from_token == "NEAR":
                        wrap_result = wrap_near(self.near_account, needed_amount)
                        execution_steps.append({
                            "step": "wrap_near",
                            "result": wrap_result,
                            "success": "final_execution_status" in wrap_result 
                                and wrap_result["final_execution_status"] == "EXECUTED_OPTIMISTIC"
                        })
                        await asyncio.sleep(15)

                    deposit_result = intent_deposit(self.near_account, from_token, needed_amount)
                    execution_steps.append({
                        "step": "deposit",
                        "result": deposit_result,
                        "success": deposit_result is not None  # deposit returns None on success
                    })
                    await asyncio.sleep(15)

                    # Verify deposit
                    new_balance = get_intent_balance(self.near_account, from_token)
                    new_balance_float = float(new_balance) if new_balance is not None else 0
                    if new_balance_float < from_amount:
                        raise ValueError(f"Deposit verification failed. Balance: {new_balance_float} {from_token}")

                # Step 3: Execute swap
                logger.info(f"Executing swap: {from_amount} {from_token} -> {to_token}")
                swap_result = intent_swap(
                    self.near_account,
                    from_token,
                    from_amount,
                    to_token,
                    chain_out=chain_out
                )
                
                if not swap_result or 'error' in swap_result or 'amount_out' not in swap_result:
                    raise Exception(f"Swap failed: {swap_result.get('error', 'Unknown error')}")
                
                # Calculate received amount
                received_amount = from_decimals(swap_result.get('amount_out', 0), to_token)
                logger.info(f"Swap successful. Received {received_amount} {to_token}")
                
                execution_steps.append({
                    "step": "swap",
                    "result": swap_result,
                    "success": True,
                    "received_amount": received_amount
                })
                
                await asyncio.sleep(15)

                # Step 4: Handle withdrawal if enabled
                if operation_details.get("destination_address"):
                    try:
                        # Check intents balance before withdrawal
                        pre_withdrawal_balance = get_intent_balance(
                            self.near_account, 
                            to_token, 
                            chain=chain_out  # Important: Check balance on destination chain
                        )
                        
                        withdrawal_result = smart_withdraw(
                            account=self.near_account,
                            token=to_token,
                            amount=received_amount,
                            destination_address=operation_details['destination_address'],
                            destination_chain=operation_details['destination_chain']
                        )
                        
                        # Wait for withdrawal to process
                        await asyncio.sleep(15)
                        
                        # Check intents balance after withdrawal
                        post_withdrawal_balance = get_intent_balance(
                            self.near_account, 
                            to_token, 
                            chain=chain_out
                        )
                        
                        withdrawal_success = (
                            withdrawal_result is not None and 
                            'error' not in withdrawal_result and
                            (post_withdrawal_balance < pre_withdrawal_balance)  # Balance should decrease
                        )
                        
                        execution_steps.append({
                            "step": "withdraw",
                            "result": withdrawal_result,
                            "success": withdrawal_success,
                            "pre_withdrawal_balance": pre_withdrawal_balance,
                            "post_withdrawal_balance": post_withdrawal_balance,
                            "destination_chain": chain_out,
                            "destination_address": operation_details['destination_address']
                        })
                        
                        if not withdrawal_success:
                            logger.warning(
                                f"Withdrawal may have failed. Pre-balance: {pre_withdrawal_balance}, "
                                f"Post-balance: {post_withdrawal_balance}, Result: {withdrawal_result}"
                            )
                            
                    except Exception as e:
                        logger.error(f"Error in withdrawal step: {e}")
                        execution_steps.append({
                            "step": "withdraw",
                            "result": {"error": str(e)},
                            "success": False
                        })
                        # Don't raise here - we want to return the swap result even if withdrawal fails

                # Return success if swap worked, even if withdrawal had issues
                return {
                    'success': True,
                    'execution_steps': execution_steps,
                    'final_result': {
                        'from_token': from_token,
                        'from_amount': from_amount,
                        'to_token': to_token,
                        'received_amount': received_amount,
                        'destination_chain': operation_details.get('destination_chain', chain_out),
                        'withdrawal_executed': bool(operation_details.get('destination_address')),
                        'withdrawal_success': execution_steps[-1].get('success') if operation_details.get('destination_address') else None
                    },
                    'execution_time': datetime.now(UTC).isoformat()
                }

            except Exception as e:
                logger.error(f"Error in execution steps: {e}", exc_info=True)
                return {
                    'success': False,
                    'error': str(e),
                    'execution_steps': execution_steps,
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

    def _validate_order(self, order: Dict) -> Dict:
        """Validate and normalize order parameters"""
        try:
            # Ensure numeric values are numbers
            order['from_amount'] = float(order['from_amount'])
            order['target_price_usd'] = float(order['target_price_usd'])
            
            # Use config's token validation for chain support
            if 'to_chain' in order:
                chain = order['to_chain'].lower()
                # Get token info to validate chain support
                token_info = get_token_by_symbol(order['to_token'], chain)
                if token_info and chain in token_info.get('chains', {}):
                    order['to_chain'] = chain
                else:
                    logger.warning(f"Token {order['to_token']} not supported on {chain}, defaulting to ethereum")
                    order['to_chain'] = 'ethereum'
            
            # Handle destination chain similarly
            if order.get('destination_address'):
                if not order.get('destination_chain'):
                    order['destination_chain'] = order['to_chain']
                else:
                    dest_chain = order['destination_chain'].lower()
                    # Validate destination chain support
                    token_info = get_token_by_symbol(order['to_token'], dest_chain)
                    if token_info and dest_chain in token_info.get('chains', {}):
                        order['destination_chain'] = dest_chain
                    else:
                        logger.warning(f"Token {order['to_token']} not supported on {dest_chain}, using to_chain")
                        order['destination_chain'] = order['to_chain']
            
            # Ensure we have proper asset IDs for the tokens
            order['from_asset_id'] = to_asset_id(order['from_token'])
            order['to_asset_id'] = to_asset_id(order['to_token'], order['to_chain'])
            
            # Ensure chain information is properly set
            if order.get('destination_address'):
                if not order.get('destination_chain'):
                    order['destination_chain'] = order['to_chain']
                dest_chain = order['destination_chain'].lower()
                # Validate destination chain support
                token_info = get_token_by_symbol(order['to_token'], dest_chain)
                if not token_info or dest_chain not in token_info.get('chains', {}):
                    raise ValueError(f"Token {order['to_token']} not supported on {dest_chain}")
                order['destination_chain'] = dest_chain
            
            return order
        except (ValueError, KeyError) as e:
            logger.error(f"Order validation failed: {e}")
            raise ValueError(f"Invalid order parameters: {e}")