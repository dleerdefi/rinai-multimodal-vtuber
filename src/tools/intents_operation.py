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
    intent_withdraw,
    intent_swap,
    get_intent_balance,
    wrap_near
)
from src.clients.near_intents_client.config import (
    get_token_by_symbol,
    to_asset_id,
    to_decimals,
    from_decimals
)

logger = logging.getLogger(__name__)

class IntentsTool(BaseTool):
    """Tool for NEAR protocol intents operations (deposit, swap, withdraw)"""
    
    # Static tool configuration
    name = "intents"
    description = "Perform token operations via NEAR intents (deposit, swap, withdraw)"
    version = "1.0"
    
    # Tool registry configuration - we'll need to add these enum values
    registry = ToolRegistry(
        content_type=ContentType.TOKEN_OPERATION,
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
        """Run the intents tool - initial entrypoint"""
        try:
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            
            if not operation or operation.get('state') == ToolOperationState.COMPLETED.value:
                # Initial analysis and command flow
                command_info = await self._analyze_command(input_data)
                
                # Based on command type, execute different flows
                if command_info["operation_type"] == "deposit":
                    result = await self._handle_deposit(command_info)
                    return result
                    
                elif command_info["operation_type"] == "withdraw":
                    result = await self._handle_withdraw(command_info)
                    return result
                    
                elif command_info["operation_type"] == "swap":
                    # For swaps, we need to generate quotes and get approval
                    quotes = await self._generate_quotes(command_info)
                    
                    # Start approval flow
                    return await self.approval_manager.start_approval_flow(
                        session_id=self.deps.session_id,
                        tool_operation_id=command_info["tool_operation_id"],
                        items=quotes["items"]
                    )
            else:
                # Let orchestrator handle ongoing operations
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
            
            # Get LLM analysis
            prompt = f"""You are a blockchain intents analyzer. Determine the limit order parameters.

Command: "{command}"

Required parameters for limit order:
   - topic: what to swap (e.g., NEAR to USDC, USDC to NEAR)
   - from_token: token to swap from (e.g., NEAR, USDC)
   - from_amount: amount to swap
   - to_token: token to swap to (e.g., NEAR, USDC)
   - min_price: minimum price in to_token per from_token
   - to_chain: chain for the output token (optional, defaults to "eth")
   - expiration_hours: hours until order expires (optional, defaults to 24)
   - slippage: slippage tolerance percentage (optional, defaults to 0.5)

Instructions:
- Return ONLY valid JSON matching the example format
- Extract all token symbols, amounts, addresses, and chains if specified
- For limit_order, min_price should be the minimum amount of to_token per from_token
- If the user specifies a price like "$3.00 / NEAR", set min_price to 3.0
- Follow the exact schema provided
- Include NO additional text or markdown

Example response format:
{{
    "tools_needed": [{{
        "tool_name": "intents",
        "action": "limit_order",
        "parameters": {{
            "topic": "NEAR to USDC",
            "from_token": "NEAR",
            "from_amount": 5.0,
            "to_token": "USDC",
            "target_price_usd": 3.0,
            "to_chain": "eth",
            "expiration_hours": 24,
            "slippage": 0.5,
            "destination_address": "0x1234567890123456789012345678901234567890", # optional
            "destination_chain": "eth" # optional
        }},
        "priority": 1
    }}],
    "reasoning": "User requested a limit order to swap 5 NEAR to USDC when the price reaches $3.00 per NEAR"
}}"""

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
                    "temperature": 0.1,
                    "max_tokens": 150
                }
            )
            
            logger.info(f"Raw LLM response: {response}")
            
            try:
                # Parse response and extract key parameters
                parsed_data = json.loads(response)
                logger.info(f"Parsed JSON data: {parsed_data}")
                
                tools_data = parsed_data.get("tools_needed", [{}])[0]
                logger.info(f"Extracted tools_data: {tools_data}")
                
                params = tools_data.get("parameters", {})
                logger.info(f"Extracted parameters: {params}")
                
                content_type = tools_data.get("content_type", "unknown")
                
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse LLM response as JSON: {e}")
                logger.error(f"Raw response that failed parsing: {response}")
                raise
            except Exception as e:
                logger.error(f"Error processing LLM response: {e}")
                raise
            
            # Set up monitoring parameters
            monitoring_params = {
                "check_interval_seconds": 60,
                "last_checked_timestamp": int(datetime.now(UTC).timestamp()),
                "best_price_seen": 0,
                "expiration_timestamp": int((datetime.now(UTC) + timedelta(hours=params.get("expiration_hours", 24))).timestamp()),
                "max_checks": 1000
            }
            
            # Create schedule for monitoring service
            schedule_id = await self.schedule_manager.initialize_schedule(
                tool_operation_id=tool_operation_id,
                schedule_info={
                    "schedule_type": "monitoring",
                    "operation_type": "limit_order",
                    "total_items": 1,
                    "monitoring_params": monitoring_params
                },
                content_type=self.registry.content_type.value,
                session_id=self.deps.session_id
            )
            
            # Create topic string for display and tracking
            topic = f"Limit order: {params['from_token']} to {params['to_token']} at ${params['target_price_usd']}"
            
            # Update operation with all necessary info
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,
                input_data={
                    "command_info": {
                        "operation_type": "limit_order",
                        "parameters": params,
                        "monitoring_params": monitoring_params,
                        "topic": topic
                    },
                    "schedule_id": schedule_id
                },
                metadata={
                    "schedule_state": ScheduleState.PENDING.value,
                    "schedule_id": schedule_id,
                    "operation_type": "limit_order"
                }
            )
            
            # Return all required information for orchestrator and managers
            return {
                # Required by orchestrator
                "tool_operation_id": tool_operation_id,
                "topic": topic,
                "item_count": 1,
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
                    "total_items": 1,
                    "monitoring_params": monitoring_params
                },
                
                # Limit order specific parameters
                "parameters": {
                    "price_oracle": {
                        "symbol": params["from_token"],
                        "target_price_usd": params["target_price_usd"]
                    },
                    "swap": {
                        "from_token": params["from_token"],
                        "from_amount": params["from_amount"],
                        "to_token": params["to_token"],
                        "chain_out": params.get("to_chain", "eth")
                    },
                    "withdraw": {
                        "enabled": bool(params.get("destination_address")),
                        "destination_address": params.get("destination_address"),
                        "destination_chain": params.get("destination_chain", "eth")
                    }
                }
            }

        except Exception as e:
            logger.error(f"Error in limit order analysis: {e}", exc_info=True)
            raise

    async def _generate_content(self, topic: str, count: int, schedule_id: str = None, tool_operation_id: str = None) -> Dict:
        """Generate human-readable content for limit order approval"""
        try:
            logger.info(f"Generating limit order content for approval: {topic}")
            
            # Get parent operation to access stored parameters
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
            
            # Get the parameters from _analyze_command
            params = operation.get("input_data", {}).get("command_info", {}).get("parameters", {})
            
            # Generate description using LLM
            prompt = f"""You are a cryptocurrency expert. Generate a detailed description for a limit order with the following parameters:

Operation Details:
- Swap {params['from_amount']} {params['from_token']} for {params['to_token']}
- Target Price: ${params['target_price_usd']} per {params['from_token']}
- Output Chain: {params.get('to_chain', 'eth')}
- Destination: {params.get('destination_address', 'default wallet')} on {params.get('destination_chain', 'eth')}
- Expires in: {params.get('expiration_hours', 24)} hours

Include:
1. A clear title summarizing the limit order
2. A detailed description of what will happen when executed
3. Important warnings about market volatility and risks
4. Expected outcome when price target is met

Format the response as JSON:
{{
    "title": "Limit Order Summary",
    "description": "Detailed description here...",
    "warnings": ["Warning 1", "Warning 2"],
    "expected_outcome": "Expected outcome description"
}}"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a cryptocurrency expert. Generate clear, detailed descriptions for limit orders."
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
                    "temperature": 0.5,
                    "max_tokens": 500
                }
            )
            
            # Parse LLM response
            generated_content = json.loads(response)
            
            # Create tool item for approval
            tool_item = {
                "session_id": self.deps.session_id,
                "tool_operation_id": tool_operation_id,
                "schedule_id": schedule_id,
                "content_type": self.registry.content_type.value,
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "content": {
                    "title": generated_content["title"],
                    "description": generated_content["description"],
                    "warnings": generated_content["warnings"],
                    "expected_outcome": generated_content["expected_outcome"],
                    "operation_details": {
                        "from_token": params["from_token"],
                        "from_amount": params["from_amount"],
                        "to_token": params["to_token"],
                        "target_price_usd": params["target_price_usd"],
                        "to_chain": params.get("to_chain", "eth"),
                        "destination_address": params.get("destination_address"),
                        "destination_chain": params.get("destination_chain", "eth"),
                        "expiration_hours": params.get("expiration_hours", 24)
                    }
                },
                "metadata": {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "monitoring_params": operation.get("input_data", {}).get("command_info", {}).get("monitoring_params", {}),
                    "state_history": [{
                        "state": ToolOperationState.COLLECTING.value,
                        "status": OperationStatus.PENDING.value,
                        "timestamp": datetime.now(UTC).isoformat()
                    }]
                }
            }
            
            # Save item
            result = await self.db.tool_items.insert_one(tool_item)
            item_id = str(result.inserted_id)
            tool_item["_id"] = item_id
            
            # Update operation with the new item
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,
                content_updates={
                    "pending_items": [item_id]
                },
                metadata={
                    "item_states": {
                        item_id: {
                            "state": ToolOperationState.COLLECTING.value,
                            "status": OperationStatus.PENDING.value
                        }
                    }
                }
            )

            return {
                "items": [tool_item],
                "schedule_id": schedule_id,
                "tool_operation_id": tool_operation_id
            }

        except Exception as e:
            logger.error(f"Error generating limit order content: {e}", exc_info=True)
            raise

    async def _handle_deposit(self, command_info: Dict) -> Dict:
        """Handle deposit operation"""
        try:
            params = command_info.get("parameters", {})
            token_symbol = params.get("token_symbol", "NEAR")
            amount = params.get("amount", 0)
            
            if amount <= 0:
                return {
                    "status": "error",
                    "response": "Invalid deposit amount. Please specify a positive amount."
                }
                
            # For NEAR deposits, we need to wrap first
            if token_symbol.upper() == "NEAR":
                try:
                    # First wrap the NEAR
                    wrap_result = await wrap_near(self.near_account, amount)
                    logger.info(f"Wrapped NEAR: {wrap_result}")
                    
                    # Small delay to ensure wrapping completes
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"Error wrapping NEAR: {e}")
                    return {
                        "status": "error",
                        "response": f"Failed to wrap NEAR: {str(e)}"
                    }
            
            # Perform the deposit
            try:
                result = await intent_deposit(self.near_account, token_symbol, amount)
                logger.info(f"Deposit result: {result}")
                
                # Update operation status
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=command_info["tool_operation_id"],
                    state=ToolOperationState.COMPLETED.value,
                    output_data={
                        "deposit_result": result
                    }
                )
                
                # Get updated balance
                try:
                    new_balance = await get_intent_balance(self.near_account, token_symbol)
                    return {
                        "status": "completed",
                        "response": f"Successfully deposited {amount} {token_symbol} to intents.near contract. Your new balance is {new_balance} {token_symbol}.",
                        "requires_tts": True,
                        "state": ToolOperationState.COMPLETED.value
                    }
                except Exception as e:
                    # Even if balance check fails, deposit was successful
                    return {
                        "status": "completed",
                        "response": f"Successfully deposited {amount} {token_symbol} to intents.near contract.",
                        "requires_tts": True,
                        "state": ToolOperationState.COMPLETED.value
                    }
                    
            except Exception as e:
                logger.error(f"Error depositing tokens: {e}")
                return {
                    "status": "error",
                    "response": f"Failed to deposit tokens: {str(e)}"
                }
                
        except Exception as e:
            logger.error(f"Error handling deposit: {e}")
            return {
                "status": "error",
                "response": f"Error handling deposit: {str(e)}"
            }

    async def _handle_withdraw(self, command_info: Dict) -> Dict:
        """Handle withdraw operation"""
        try:
            params = command_info.get("parameters", {})
            token_symbol = params.get("token_symbol", "NEAR")
            amount = params.get("amount", 0)
            destination_address = params.get("destination_address", self.near_account.account_id)
            destination_chain = params.get("destination_chain", "near")
            
            if amount <= 0:
                return {
                    "status": "error",
                    "response": "Invalid withdrawal amount. Please specify a positive amount."
                }
                
            # Check current balance
            current_balance = await get_intent_balance(self.near_account, token_symbol)
            if current_balance < amount:
                return {
                    "status": "error",
                    "response": f"Insufficient balance. You have {current_balance} {token_symbol}, but requested to withdraw {amount}."
                }
            
            # Perform the withdrawal
            try:
                result = await intent_withdraw(
                    self.near_account, 
                    destination_address, 
                    token_symbol, 
                    amount, 
                    network=destination_chain
                )
                logger.info(f"Withdrawal result: {result}")
                
                # Update operation status
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=command_info["tool_operation_id"],
                    state=ToolOperationState.COMPLETED.value,
                    output_data={
                        "withdrawal_result": result
                    }
                )
                
                # Get updated balance
                try:
                    new_balance = await get_intent_balance(self.near_account, token_symbol)
                    return {
                        "status": "completed",
                        "response": f"Successfully withdrew {amount} {token_symbol} to {destination_address} on {destination_chain}. Your remaining balance is {new_balance} {token_symbol}.",
                        "requires_tts": True,
                        "state": ToolOperationState.COMPLETED.value
                    }
                except Exception as e:
                    # Even if balance check fails, withdrawal was successful
                    return {
                        "status": "completed",
                        "response": f"Successfully withdrew {amount} {token_symbol} to {destination_address} on {destination_chain}.",
                        "requires_tts": True,
                        "state": ToolOperationState.COMPLETED.value
                    }
                    
            except Exception as e:
                logger.error(f"Error withdrawing tokens: {e}")
                return {
                    "status": "error",
                    "response": f"Failed to withdraw tokens: {str(e)}"
                }
                
        except Exception as e:
            logger.error(f"Error handling withdrawal: {e}")
            return {
                "status": "error",
                "response": f"Error handling withdrawal: {str(e)}"
            }

    async def _generate_quotes(self, command_info: Dict) -> Dict:
        """Generate quotes for swap operation using Solver Bus"""
        try:
            params = command_info.get("parameters", {})
            from_token = params.get("from_token", "NEAR")
            from_amount = params.get("from_amount", 0)
            to_token = params.get("to_token", "USDC")
            to_chain = params.get("to_chain", "near")
            
            # Convert tokens to asset identifiers
            try:
                from_asset_id = await self._get_asset_id(from_token)
                to_asset_id = await self._get_asset_id(to_token)
            except Exception as e:
                logger.error(f"Error converting tokens to asset IDs: {e}")
                from_asset_id = f"nep141:{from_token.lower()}.near"
                to_asset_id = f"nep141:{to_token.lower()}.near"
            
            # Handle deposit if needed
            deposit_item = None
            if command_info.get("needs_deposit", False):
                deposit_amount = command_info.get("deposit_amount", 0)
                
                # Create item for deposit approval
                deposit_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": command_info["tool_operation_id"],
                    "content_type": self.registry.content_type.value,
                    "state": ToolOperationState.COLLECTING.value,
                    "status": OperationStatus.PENDING.value,
                    "content": {
                        "operation_type": "deposit",
                        "token_symbol": from_token,
                        "amount": deposit_amount,
                        "description": f"Deposit {deposit_amount} {from_token} to enable swap"
                    },
                    "metadata": {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "step": "deposit"
                    }
                }
                
                # Save deposit item
                deposit_result = await self.db.tool_items.insert_one(deposit_item)
                deposit_item_id = str(deposit_result.inserted_id)
                deposit_item["_id"] = deposit_item_id
                
                # Update operation with deposit item
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=command_info["tool_operation_id"],
                    metadata={
                        "needs_deposit": True,
                        "deposit_amount": deposit_amount,
                        "deposit_item_id": deposit_item_id
                    }
                )
            
            # Generate quotes using Solver Bus if available
            solver_quotes = []
            if self.solver_bus_client:
                try:
                    # Convert amount to proper decimal format
                    decimal_amount = str(int(from_amount * 10**24))  # Assuming NEAR with 24 decimals
                    
                    # Get quotes from Solver Bus
                    quote_result = await self.solver_bus_client.get_quote(
                        token_in=from_asset_id,
                        token_out=to_asset_id,
                        amount_in=decimal_amount
                    )
                    
                    if quote_result.get("success", False):
                        solver_quotes = quote_result.get("quotes", [])
                        logger.info(f"Received {len(solver_quotes)} quotes from Solver Bus")
                except Exception as e:
                    logger.error(f"Error getting quotes from Solver Bus: {e}")
            
            # Create swap items based on quotes or fallback to CoinGecko estimate
            swap_items = []
            
            if solver_quotes:
                # Create items from solver quotes
                for i, quote in enumerate(solver_quotes):
                    # Extract quote data
                    quote_hash = quote.get("quote_hash", "")
                    amount_in = quote.get("amount_in", "0")
                    amount_out = quote.get("amount_out", "0")
                    expiration_time = quote.get("expiration_time", 0)
                    
                    # Convert amounts to human-readable format
                    try:
                        human_amount_in = float(amount_in) / 10**24  # Assuming NEAR with 24 decimals
                        human_amount_out = float(amount_out) / 10**6  # Assuming USDC with 6 decimals
                        rate = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                    except (ValueError, ZeroDivisionError):
                        human_amount_in = float(amount_in)
                        human_amount_out = float(amount_out)
                        rate = 0
                    
                    # Format expiration time
                    expiration_datetime = datetime.fromtimestamp(int(expiration_time))
                    expiration_str = expiration_datetime.strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Create swap item
                    swap_item = {
                        "session_id": self.deps.session_id,
                        "tool_operation_id": command_info["tool_operation_id"],
                        "content_type": self.registry.content_type.value,
                        "state": ToolOperationState.COLLECTING.value,
                        "status": OperationStatus.PENDING.value,
                        "content": {
                            "operation_type": "swap",
                            "from_token": from_token,
                            "from_amount": human_amount_in,
                            "to_token": to_token,
                            "to_chain": to_chain,
                            "received_amount": human_amount_out,
                            "exchange_rate": rate,
                            "quote_hash": quote_hash,
                            "expiration_time": expiration_str,
                            "description": f"Swap {human_amount_in} {from_token} for {human_amount_out} {to_token} on {to_chain} (Rate: 1 {from_token} = {rate} {to_token})"
                        },
                        "metadata": {
                            "generated_at": datetime.now(UTC).isoformat(),
                            "step": "swap",
                            "source": "solver_bus",
                            "quote_data": quote
                        }
                    }
                    
                    # Save swap item
                    swap_result = await self.db.tool_items.insert_one(swap_item)
                    swap_item_id = str(swap_result.inserted_id)
                    swap_item["_id"] = swap_item_id
                    swap_items.append(swap_item)
            else:
                # Fallback to CoinGecko estimate
                estimated_rate = command_info.get("estimated_rate", 0)
                estimated_receive = command_info.get("estimated_receive", 0)
                
                # Create fallback swap item
                swap_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": command_info["tool_operation_id"],
                    "content_type": self.registry.content_type.value,
                    "state": ToolOperationState.COLLECTING.value,
                    "status": OperationStatus.PENDING.value,
                    "content": {
                        "operation_type": "swap",
                        "from_token": from_token,
                        "from_amount": from_amount,
                        "to_token": to_token,
                        "to_chain": to_chain,
                        "estimated_rate": estimated_rate,
                        "estimated_receive": estimated_receive,
                        "description": f"Swap {from_amount} {from_token} for approximately {estimated_receive} {to_token} on {to_chain} (Estimated only)"
                    },
                    "metadata": {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "step": "swap",
                        "source": "coingecko_estimate",
                        "is_fallback": True
                    }
                }
                
                # Save swap item
                swap_result = await self.db.tool_items.insert_one(swap_item)
                swap_item_id = str(swap_result.inserted_id)
                swap_item["_id"] = swap_item_id
                swap_items.append(swap_item)
            
            # Update operation with all swap items
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=command_info["tool_operation_id"],
                metadata={
                    "swap_item_ids": [str(item["_id"]) for item in swap_items],
                    "quote_count": len(swap_items)
                }
            )
            
            # Prepare items for approval flow
            items = []
            
            # Add deposit item if needed
            if deposit_item:
                items.append(deposit_item)
            
            # Add swap items
            items.extend(swap_items)
            
            # Create analysis for display
            analysis = {
                "total_quotes": len(swap_items),
                "deposit_required": deposit_item is not None,
                "quote_source": "solver_bus" if solver_quotes else "coingecko_estimate",
                "from_token": from_token,
                "to_token": to_token,
                "to_chain": to_chain
            }
            
            return {
                "success": True,
                "items": items,
                "tool_operation_id": command_info["tool_operation_id"],
                "analysis": analysis
            }
            
        except Exception as e:
            logger.error(f"Error generating quotes: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    # Add execute_scheduled_operation method to align with schedule_manager.py expectations
    async def execute_scheduled_operation(self, operation: Dict) -> Dict:
        """Execute a scheduled operation when triggered by monitoring service
        
        This method is called by ScheduleManager when a scheduled operation
        is due for execution, either based on time or when monitoring conditions are met.
        """
        try:
            logger.info(f"Executing scheduled operation: {operation.get('_id')}")
            
            # Extract operation details
            content = operation.get("content", {})
            operation_type = content.get("operation_type")
            
            # Get the favorable quote if this is a limit order
            favorable_quote = operation.get("metadata", {}).get("favorable_quote")
            
            # Execute based on operation type
            if operation_type == "deposit":
                token_symbol = content.get("token_symbol")
                amount = content.get("amount")
                
                # For NEAR deposits, we need to wrap first
                if token_symbol.upper() == "NEAR":
                    try:
                        # First wrap the NEAR
                        wrap_result = await wrap_near(self.near_account, amount)
                        logger.info(f"Wrapped NEAR: {wrap_result}")
                        
                        # Small delay to ensure wrapping completes
                        await asyncio.sleep(3)
                    except Exception as e:
                        logger.error(f"Error wrapping NEAR: {e}")
                        return {
                            "success": False,
                            "message": f"Failed to wrap NEAR: {str(e)}"
                        }
                
                # Perform the deposit
                try:
                    result = await intent_deposit(self.near_account, token_symbol, amount)
                    logger.info(f"Deposit result: {result}")
                    
                    return {
                        "success": True,
                        "message": f"Successfully deposited {amount} {token_symbol}",
                        "result": result
                    }
                except Exception as e:
                    logger.error(f"Error depositing tokens: {e}")
                    return {
                        "success": False,
                        "message": f"Failed to deposit tokens: {str(e)}"
                    }
                    
            elif operation_type == "withdraw":
                token_symbol = content.get("token_symbol")
                amount = content.get("amount")
                destination_address = content.get("destination_address", self.near_account.account_id)
                destination_chain = content.get("destination_chain", "near")
                
                # Perform the withdrawal
                try:
                    result = await intent_withdraw(
                        self.near_account, 
                        destination_address, 
                        token_symbol, 
                        amount, 
                        network=destination_chain
                    )
                    logger.info(f"Withdrawal result: {result}")
                    
                    return {
                        "success": True,
                        "message": f"Successfully withdrew {amount} {token_symbol} to {destination_address} on {destination_chain}",
                        "result": result
                    }
                except Exception as e:
                    logger.error(f"Error withdrawing tokens: {e}")
                    return {
                        "success": False,
                        "message": f"Failed to withdraw tokens: {str(e)}"
                    }
                    
            elif operation_type == "swap" or operation_type == "limit_order":
                from_token = content.get("from_token")
                from_amount = content.get("from_amount")
                to_token = content.get("to_token")
                to_chain = content.get("to_chain", "near")
                
                # Execute the swap
                try:
                    # Use the quote hash if available from monitoring service
                    quote_hash = None
                    if favorable_quote:
                        quote_hash = favorable_quote.get("quote_hash")
                        logger.info(f"Using favorable quote with hash: {quote_hash}")
                    
                    swap_result = await intent_swap(
                        self.near_account, 
                        from_token, 
                        from_amount, 
                        to_token, 
                        chain_out=to_chain
                    )
                    
                    # Calculate amount received
                    amount_out = from_decimals(swap_result.get('amount_out', 0), to_token)
                    
                    return {
                        "success": True,
                        "message": f"Successfully swapped {from_amount} {from_token} for {amount_out} {to_token}",
                        "result": swap_result
                    }
                except Exception as e:
                    logger.error(f"Error executing swap: {e}")
                    return {
                        "success": False,
                        "message": f"Failed to execute swap: {str(e)}"
                    }
            else:
                return {
                    "success": False,
                    "message": f"Unknown operation type: {operation_type}"
                }
                
        except Exception as e:
            logger.error(f"Error in execute_scheduled_operation: {e}")
            return {
                "success": False,
                "message": f"Error executing scheduled operation: {str(e)}"
            }

    async def _get_token_price(self, symbol: str) -> Optional[float]:
        """Get token price using CoinGecko"""
        try:
            if not self.coingecko_client:
                return None
                
            # Get CoinGecko ID for the token
            coingecko_id = await self.coingecko_client._get_coingecko_id(symbol)
            if not coingecko_id:
                return None
                
            # Get token price
            price_data = await self.coingecko_client.get_token_price(coingecko_id)
            if not price_data or 'price_usd' not in price_data:
                return None
                
            return price_data['price_usd']
            
        except Exception as e:
            logger.error(f"Error getting token price: {e}")
            return None

    async def _get_asset_id(self, token_symbol: str) -> str:
        """Convert token symbol to defuse asset identifier"""
        token_symbol = token_symbol.upper()
        
        # Use the to_asset_id function from config if available
        try:
            if 'to_asset_id' in globals() or hasattr(self, 'to_asset_id'):
                if hasattr(self, 'to_asset_id'):
                    return self.to_asset_id(token_symbol)
                else:
                    return to_asset_id(token_symbol)
        except Exception as e:
            logger.error(f"Error using to_asset_id: {e}")
        
        # Fallback mappings
        mappings = {
            "NEAR": "nep141:wrap.near",
            "USDC": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",  # NEAR-USDC
            "USDC.E": "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near",  # ETH-USDC
            "USDT": "nep141:dac17f958d2ee523a2206206994597c13d831ec7.factory.bridge.near",
            "ETH": "nep141:eth.near",
            "BTC": "nep141:btc.near",
        }
        
        return mappings.get(token_symbol, f"nep141:{token_symbol.lower()}.near")