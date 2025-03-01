import asyncio
import logging
from datetime import datetime, UTC, timedelta
import time
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from typing import Dict, List, Optional, Any

from src.db.db_schema import RinDB
from src.db.enums import OperationStatus, ToolOperationState
from src.managers.tool_state_manager import ToolStateManager
from src.clients.near_intents_client.intents_client import (
    intent_swap,
    get_intent_balance,
    create_token_diff_quote,
    publish_intent,
    IntentRequest,
    fetch_options,
    select_best_option
)
from src.clients.near_intents_client.config import (
    get_token_by_symbol,
    to_asset_id,
    to_decimals,
    from_decimals
)

logger = logging.getLogger(__name__)

class LimitOrderMonitoringService:
    """Service for monitoring and executing limit orders when conditions are met"""
    
    def __init__(self, mongo_uri: str, orchestrator=None):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = RinDB(self.mongo_client)
        self.tool_state_manager = ToolStateManager(db=self.db)
        
        # Will be injected
        self.near_account = None
        self.solver_bus_client = None
        
        self.running = False
        self._task = None
        self._check_interval = 30  # Default check interval in seconds

    async def inject_dependencies(self, **services):
        """Inject required services"""
        self.near_account = services.get("near_account")
        self.solver_bus_client = services.get("solver_bus_client")
        
        if not self.near_account:
            logger.error("NEAR account dependency not injected")
        if not self.solver_bus_client:
            logger.warning("Solver Bus client dependency not injected, will use fallback quote method")

    async def start(self):
        """Start the limit order monitoring service"""
        if self.running:
            return
        
        await self.db.initialize()  # Initialize RinDB
        self.running = True
        self._task = asyncio.create_task(self._monitoring_loop())
        logger.info("Limit order monitoring service started")

    async def _monitoring_loop(self):
        """Main monitoring loop that checks for limit orders"""
        while self.running:
            try:
                # Get current time
                current_time = datetime.now(UTC)
                
                # Get all active limit orders
                active_orders = await self.db.tool_items.find({
                    "content.operation_type": "limit_order",
                    "status": OperationStatus.SCHEDULED.value,
                    "state": ToolOperationState.EXECUTING.value
                }).to_list(None)
                
                if active_orders:
                    logger.info(f"Found {len(active_orders)} active limit orders to check at {current_time.isoformat()}")
                    
                    for order in active_orders:
                        try:
                            # Check if it's time to check this order based on its check interval
                            params = order.get("parameters", {}).get("custom_params", {})
                            last_checked = params.get("last_checked_timestamp", 0)
                            check_interval = params.get("check_interval_seconds", 60)
                            
                            if time.time() - last_checked >= check_interval:
                                await self._check_limit_order(order)
                            
                        except Exception as e:
                            logger.error(f"Error checking limit order {order.get('_id')}: {e}", exc_info=True)
                
                # Sleep before next check
                await asyncio.sleep(self._check_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Longer wait on error

    async def _check_limit_order(self, order):
        """Check if a limit order's conditions are met"""
        try:
            order_id = str(order.get('_id'))
            content = order.get("content", {})
            params = order.get("parameters", {}).get("custom_params", {})
            
            # Extract order parameters
            from_token = content.get("from_token")
            from_amount = content.get("from_amount")
            to_token = content.get("to_token")
            to_chain = content.get("to_chain", "eth")  # Default to ETH chain for USDC
            min_price = content.get("min_price")
            
            logger.info(f"Checking limit order {order_id}: {from_amount} {from_token} -> {to_token} at min price {min_price}")
            
            # Check if order has expired
            expiration_timestamp = params.get("expiration_timestamp")
            if expiration_timestamp and time.time() > expiration_timestamp:
                await self._expire_limit_order(order)
                return
            
            # Check balance to ensure we have enough funds
            try:
                current_balance = await get_intent_balance(self.near_account, from_token)
                if current_balance < from_amount:
                    logger.warning(f"Insufficient balance for limit order {order_id}. Have {current_balance} {from_token}, need {from_amount}")
                    
                    # Update order with warning
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_warning": f"Insufficient balance: {current_balance} {from_token}",
                            "metadata.last_warning_time": datetime.now(UTC).isoformat()
                        }}
                    )
                    return
            except Exception as e:
                logger.error(f"Error checking balance for limit order {order_id}: {e}")
            
            # Get current quote using the approach from test_solver_quotes_simple.py
            try:
                # Create intent request using the proper class
                request = IntentRequest()
                request.asset_in(from_token, from_amount)
                request.asset_out(to_token, chain=to_chain)
                
                logger.info(f"Getting quotes for {from_amount} {from_token} to {to_token}")
                logger.info(f"Asset IDs: {request.asset_in['asset']} -> {request.asset_out['asset']}")
                
                # Get quotes using the fetch_options function that works
                solver_quotes = fetch_options(request)
                
                if not solver_quotes:
                    logger.info(f"No quotes available for limit order {order_id}")
                    
                    # Update last checked timestamp
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": "No quotes available"
                        }}
                    )
                    return
                
                # Find best quote using the existing function
                best_option = select_best_option(solver_quotes)
                
                if not best_option:
                    logger.warning(f"No valid quotes for limit order {order_id}")
                    
                    # Update last checked timestamp
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": "No valid quotes found"
                        }}
                    )
                    return
                
                # Calculate current price
                from_token_info = get_token_by_symbol(from_token)
                to_token_info = get_token_by_symbol(to_token)
                
                from_decimals_val = from_token_info.get('decimals', 24) if from_token_info else 24
                to_decimals_val = to_token_info.get('decimals', 6) if to_token_info else 6
                
                human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals_val)
                human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals_val)
                
                current_price = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                
                logger.info(f"Limit order {order_id} current price: {current_price}, min price: {min_price}")
                
                # Update best quote seen if this is better
                if current_price > params.get("best_price_seen", 0):
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.best_price_seen": current_price,
                            "parameters.custom_params.best_quote_seen": best_option,
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": f"New best price: {current_price}"
                        }}
                    )
                else:
                    # Just update last checked timestamp
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": f"Current price: {current_price}"
                        }}
                    )
                
                # Check if price condition is met
                if current_price >= min_price:
                    logger.info(f"Limit order {order_id} conditions met! Current price: {current_price}, Min price: {min_price}")
                    
                    # Store the quote for execution
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.execution_quote": best_option,
                            "parameters.custom_params.execution_price": current_price,
                            "parameters.custom_params.quote_payload": {
                                "from_token": from_token,
                                "from_amount": from_amount,
                                "to_token": to_token,
                                "to_chain": to_chain,
                                "quote_asset_in": best_option.get('defuse_asset_identifier_in'),
                                "quote_asset_out": best_option.get('defuse_asset_identifier_out'),
                                "amount_in": best_option.get('amount_in'),
                                "amount_out": best_option.get('amount_out'),
                                "quote_hash": best_option.get('quote_hash')
                            },
                            "parameters.custom_params.swap_payload": {
                                "from_token": from_token,
                                "from_amount": from_amount,
                                "to_token": to_token,
                                "to_chain": to_chain
                            }
                        }}
                    )
                    
                    # Execute the order
                    await self._execute_limit_order(order, best_option)
                
            except Exception as e:
                logger.error(f"Error processing quotes for limit order {order_id}: {e}", exc_info=True)
                
                # Update with error
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "parameters.custom_params.last_checked_timestamp": int(time.time()),
                        "metadata.last_error": f"Error processing quotes: {str(e)}",
                        "metadata.last_error_time": datetime.now(UTC).isoformat(),
                        "metadata.error_count": order.get("metadata", {}).get("error_count", 0) + 1
                    }}
                )
            
        except Exception as e:
            logger.error(f"Error checking limit order: {e}", exc_info=True)

    async def _execute_limit_order(self, order, quote):
        """Execute a limit order when conditions are met"""
        try:
            order_id = str(order.get('_id'))
            content = order.get("content", {})
            params = order.get("parameters", {}).get("custom_params", {})
            
            # Extract order parameters
            from_token = content.get("from_token")
            from_amount = content.get("from_amount")
            to_token = content.get("to_token")
            to_chain = content.get("to_chain", "eth")
            
            logger.info(f"Executing limit order {order_id}: {from_amount} {from_token} -> {to_token}")
            
            # Update order status to executing
            await self.db.tool_items.update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {
                    "status": OperationStatus.EXECUTING.value,
                    "metadata.execution_started_at": datetime.now(UTC).isoformat()
                }}
            )
            
            # Execute the swap using the stored quote
            try:
                # First try using the quote hash and publish intent
                try:
                    # Get the quote payload from the database
                    quote_payload = params.get("quote_payload", {})
                    if not quote_payload:
                        logger.warning(f"No quote payload found for order {order_id}, using provided quote")
                        quote_payload = {
                            "from_token": from_token,
                            "from_amount": from_amount,
                            "to_token": to_token,
                            "to_chain": to_chain,
                            "quote_asset_in": quote.get('defuse_asset_identifier_in'),
                            "quote_asset_out": quote.get('defuse_asset_identifier_out'),
                            "amount_in": quote.get('amount_in'),
                            "amount_out": quote.get('amount_out'),
                            "quote_hash": quote.get('quote_hash')
                        }
                    
                    # Create the quote
                    signed_quote = await create_token_diff_quote(
                        self.near_account,
                        quote_payload.get("from_token", from_token),
                        quote_payload.get("amount_in", quote.get('amount_in')),
                        quote_payload.get("to_token", to_token),
                        quote_payload.get("amount_out", quote.get('amount_out')),
                        quote_asset_in=quote_payload.get("quote_asset_in", quote.get('defuse_asset_identifier_in')),
                        quote_asset_out=quote_payload.get("quote_asset_out", quote.get('defuse_asset_identifier_out'))
                    )
                    
                    # Publish the intent
                    signed_intent = {
                        "signed_data": signed_quote,
                        "quote_hashes": [quote_payload.get("quote_hash", quote.get('quote_hash'))]
                    }
                    
                    swap_result = await publish_intent(signed_intent)
                    
                    logger.info(f"Limit order {order_id} executed with quote hash: {swap_result}")
                    
                    # Update order status
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "status": OperationStatus.EXECUTED.value,
                            "state": ToolOperationState.COMPLETED.value,
                            "executed_time": datetime.now(UTC),
                            "api_response": {
                                "success": True,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "result": swap_result
                            },
                            "metadata.execution_result": swap_result,
                            "metadata.execution_completed_at": datetime.now(UTC).isoformat(),
                            "metadata.execution_method": "quote_hash"
                        }}
                    )
                    
                    return swap_result
                    
                except Exception as e:
                    logger.warning(f"Failed to execute limit order {order_id} with quote hash: {e}")
                    
                    # Fall back to direct swap method
                    logger.info(f"Falling back to direct swap for limit order {order_id}")
                    
                    # Get the swap payload from the database
                    swap_payload = params.get("swap_payload", {})
                    if not swap_payload:
                        logger.warning(f"No swap payload found for order {order_id}, using original parameters")
                        swap_payload = {
                            "from_token": from_token,
                            "from_amount": from_amount,
                            "to_token": to_token,
                            "to_chain": to_chain
                        }
                    
                    swap_result = await intent_swap(
                        self.near_account,
                        swap_payload.get("from_token", from_token),
                        swap_payload.get("from_amount", from_amount),
                        swap_payload.get("to_token", to_token),
                        chain_out=swap_payload.get("to_chain", to_chain)
                    )
                    
                    logger.info(f"Limit order {order_id} executed with direct swap: {swap_result}")
                    
                    # Update order status
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "status": OperationStatus.EXECUTED.value,
                            "state": ToolOperationState.COMPLETED.value,
                            "executed_time": datetime.now(UTC),
                            "api_response": {
                                "success": True,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "result": swap_result
                            },
                            "metadata.execution_result": swap_result,
                            "metadata.execution_completed_at": datetime.now(UTC).isoformat(),
                            "metadata.execution_method": "direct_swap"
                        }}
                    )
                    
                    return swap_result
                    
            except Exception as e:
                logger.error(f"Error executing limit order {order_id}: {e}", exc_info=True)
                
                # Update order with error
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "status": OperationStatus.FAILED.value,
                        "metadata.execution_error": str(e),
                        "metadata.execution_error_time": datetime.now(UTC).isoformat()
                    }}
                )
                
                return {
                    "success": False,
                    "error": str(e)
                }
                
        except Exception as e:
            logger.error(f"Error in _execute_limit_order: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    async def _execute_direct_swap(self, order):
        """Execute a limit order using direct swap method"""
        try:
            order_id = str(order.get('_id'))
            content = order.get("content", {})
            
            # Extract order parameters
            from_token = content.get("from_token")
            from_amount = content.get("from_amount")
            to_token = content.get("to_token")
            to_chain = content.get("to_chain", "near")
            
            logger.info(f"Executing direct swap for limit order {order_id}: {from_amount} {from_token} -> {to_token}")
            
            # Update order status to executing
            await self.db.tool_items.update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {
                    "status": OperationStatus.EXECUTING.value,
                    "metadata.execution_started_at": datetime.now(UTC).isoformat()
                }}
            )
            
            # Execute the swap directly
            try:
                swap_result = await intent_swap(
                    self.near_account,
                    from_token,
                    from_amount,
                    to_token,
                    chain_out=to_chain
                )
                
                logger.info(f"Limit order {order_id} executed with direct swap: {swap_result}")
                
                # Update order status
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "status": OperationStatus.EXECUTED.value,
                        "state": ToolOperationState.COMPLETED.value,
                        "executed_time": datetime.now(UTC),
                        "api_response": {
                            "success": True,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "result": swap_result
                        },
                        "metadata.execution_result": swap_result,
                        "metadata.execution_completed_at": datetime.now(UTC).isoformat(),
                        "metadata.execution_method": "direct_swap"
                    }}
                )
                
                return swap_result
                
            except Exception as e:
                logger.error(f"Error executing direct swap for limit order {order_id}: {e}", exc_info=True)
                
                # Update order with error
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "status": OperationStatus.FAILED.value,
                        "metadata.execution_error": str(e),
                        "metadata.execution_error_time": datetime.now(UTC).isoformat()
                    }}
                )
                
                return {
                    "success": False,
                    "error": str(e)
                }
                
        except Exception as e:
            logger.error(f"Error in _execute_direct_swap: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    async def _expire_limit_order(self, order):
        """Mark a limit order as expired"""
        try:
            order_id = str(order.get('_id'))
            logger.info(f"Marking limit order {order_id} as expired")
            
            # Update order status to expired
            await self.db.tool_items.update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {
                    "status": OperationStatus.FAILED.value,
                    "state": ToolOperationState.ERROR.value,
                    "metadata.expired_at": datetime.now(UTC).isoformat(),
                    "metadata.best_price_seen": order.get("parameters", {}).get("custom_params", {}).get("best_price_seen", 0)
                }}
            )
            
        except Exception as e:
            logger.error(f"Error marking limit order as expired: {e}", exc_info=True)

    async def register_limit_order(self, order_id: str, params: Dict):
        """Register a new limit order with the monitoring service"""
        try:
            logger.info(f"Registering limit order {order_id} with monitoring service")
            
            # Get the order from the database
            order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
            if not order:
                logger.error(f"Order {order_id} not found in database")
                return False
                
            # Set up monitoring parameters
            monitoring_params = {
                "check_interval_seconds": params.get("check_interval_seconds", 60),
                "last_checked_timestamp": int(time.time()),
                "best_price_seen": 0,
                "expiration_timestamp": int(time.time()) + params.get("expiration_seconds", 86400),  # Default 24 hours
                "max_checks": params.get("max_checks", 1000)
            }
            
            # Update the order with monitoring parameters
            await self.db.tool_items.update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {
                    "status": OperationStatus.SCHEDULED.value,
                    "parameters.custom_params": monitoring_params,
                    "metadata.monitoring_started_at": datetime.now(UTC).isoformat(),
                    "metadata.monitoring_expiration": datetime.fromtimestamp(monitoring_params["expiration_timestamp"], UTC).isoformat()
                }}
            )
            
            logger.info(f"Limit order {order_id} registered for monitoring until {monitoring_params['expiration_timestamp']}")
            return True
            
        except Exception as e:
            logger.error(f"Error registering limit order: {e}", exc_info=True)
            return False

    async def stop(self):
        """Stop the limit order monitoring service"""
        if not self.running:
            return
            
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Limit order monitoring service stopped")

    async def get_active_orders(self):
        """Get all active limit orders"""
        try:
            active_orders = await self.db.tool_items.find({
                "content.operation_type": "limit_order",
                "status": OperationStatus.SCHEDULED.value,
                "state": ToolOperationState.EXECUTING.value
            }).to_list(None)
            
            return active_orders
        except Exception as e:
            logger.error(f"Error getting active orders: {e}", exc_info=True)
            return []

    async def get_order_status(self, order_id: str):
        """Get the status of a specific limit order"""
        try:
            order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
            if not order:
                return {
                    "success": False,
                    "error": "Order not found"
                }
                
            return {
                "success": True,
                "order_id": order_id,
                "status": order.get("status"),
                "state": order.get("state"),
                "from_token": order.get("content", {}).get("from_token"),
                "to_token": order.get("content", {}).get("to_token"),
                "min_price": order.get("content", {}).get("min_price"),
                "best_price_seen": order.get("parameters", {}).get("custom_params", {}).get("best_price_seen", 0),
                "last_checked": order.get("parameters", {}).get("custom_params", {}).get("last_checked_timestamp", 0),
                "expiration": order.get("parameters", {}).get("custom_params", {}).get("expiration_timestamp", 0)
            }
        except Exception as e:
            logger.error(f"Error getting order status: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }