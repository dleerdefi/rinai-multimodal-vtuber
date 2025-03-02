import asyncio
import logging
from datetime import datetime, UTC, timedelta
import time
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from typing import Dict, List, Optional, Any

from src.db.db_schema import RinDB
from src.db.enums import OperationStatus, ToolOperationState, ContentType
from src.managers.tool_state_manager import ToolStateManager
from src.managers.schedule_manager import ScheduleManager
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
from src.clients.coingecko_client import CoinGeckoClient
from src.clients.near_account_helper import get_near_account

logger = logging.getLogger(__name__)

class LimitOrderMonitoringService:
    """Service for monitoring and executing limit orders when conditions are met"""
    
    def __init__(self, mongo_uri: str, schedule_manager: ScheduleManager = None):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = RinDB(self.mongo_client)
        self.tool_state_manager = ToolStateManager(db=self.db)
        self.schedule_manager = schedule_manager
        
        # Will be injected
        self.near_account = None
        self.coingecko_client = None
        
        # Add tool registry similar to schedule_service
        self._tools = {}
        
        self.running = False
        self._task = None
        self._check_interval = 30  # Default check interval in seconds

    async def inject_dependencies(self, **services):
        """Inject required services"""
        self.near_account = services.get("near_account")
        self.coingecko_client = services.get("coingecko_client")
        self.schedule_manager = services.get("schedule_manager")
        
        # Get the IntentsTool from the services
        intents_tool = services.get("intents_tool")
        if intents_tool:
            # Register the tool by its content type
            self._tools[ContentType.LIMIT_ORDER.value] = intents_tool
            self._tools['limit_order'] = intents_tool  # Add string version for flexibility
            logger.info("Registered IntentsTool for limit order monitoring")
        
        if not self.near_account:
            logger.error("NEAR account dependency not injected - limit order execution will fail")
        else:
            logger.info("NEAR account dependency successfully injected")
        if not self.coingecko_client:
            logger.error("CoinGecko client dependency not injected")
        if not self.schedule_manager:
            logger.error("Schedule manager dependency not injected")

    def _get_tool_for_content(self, content_type: str) -> Optional[Any]:
        """Get appropriate tool for content type"""
        try:
            # Normalize content type string
            if isinstance(content_type, ContentType):
                content_type = content_type.value
            
            # Check registry for tool
            tool = self._tools.get(content_type)
            
            # If not found, log more detailed information
            if not tool:
                logger.error(f"No tool found for content type: {content_type}")
                logger.error(f"Available content types in tool registry: {list(self._tools.keys())}")
                
                # Try to get the tool from schedule_manager's tool_registry as fallback
                if self.schedule_manager and hasattr(self.schedule_manager, 'tool_registry'):
                    tool = self.schedule_manager.tool_registry.get(content_type)
                    if tool:
                        logger.info(f"Found tool for content type {content_type} in schedule_manager's tool_registry")
                        # Cache it for future use
                        self._tools[content_type] = tool
                        return tool
            
            return tool
        except Exception as e:
            logger.error(f"Error getting tool for content type {content_type}: {e}")
            return None

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
                
                # Get all active limit orders - SPECIFICALLY those marked as monitored
                active_orders = await self.db.tool_items.find({
                    "content_type": ContentType.LIMIT_ORDER.value,
                    "status": OperationStatus.SCHEDULED.value,
                    "state": ToolOperationState.EXECUTING.value,
                    "metadata.scheduling_type": "monitored"  # Only get monitored items
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
        """Check if a limit order's conditions are met using CoinGecko USD prices"""
        try:
            order_id = str(order.get('_id'))
            content = order.get("content", {})
            operation_details = content.get("operation_details", {})
            params = order.get("parameters", {}).get("custom_params", {})
            
            # Get values from operation_details
            from_token = operation_details.get("from_token")
            from_amount = str(operation_details.get("from_amount"))  # Convert to string
            to_token = operation_details.get("to_token")
            target_price_usd = float(operation_details.get("target_price_usd", 0))  # Ensure float for comparison
            
            logger.info(f"Checking limit order {order_id}: {from_amount} {from_token} target price ${target_price_usd}")
            
            # Check if we have the required parameters
            if not from_token or not target_price_usd:
                logger.error(f"Missing required parameters for limit order {order_id}: from_token={from_token}, target_price_usd={target_price_usd}")
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "parameters.custom_params.last_checked_timestamp": int(time.time()),
                        "metadata.last_error": f"Missing required parameters: from_token={from_token}, target_price_usd={target_price_usd}",
                        "metadata.last_error_time": datetime.now(UTC).isoformat()
                    }}
                )
                return

            # Check if order has expired
            expiration_timestamp = params.get("expiration_timestamp")
            if expiration_timestamp and time.time() > expiration_timestamp:
                await self._expire_limit_order(order)
                return
            
            # Get current USD price from CoinGecko
            try:
                coingecko_id = await self.coingecko_client._get_coingecko_id(from_token)
                if not coingecko_id:
                    logger.error(f"Could not find CoinGecko ID for {from_token}")
                    return
                
                price_data = await self.coingecko_client.get_token_price(coingecko_id)
                if not price_data or 'price_usd' not in price_data:
                    logger.error(f"Could not get price data for {from_token}")
                    return
                
                current_price = float(price_data['price_usd'])  # Ensure float for comparison
                
                logger.info(f"Current {from_token} price: ${current_price}, Target: ${target_price_usd}")
                
                # Update best price seen if this is better
                if current_price > params.get("best_price_seen", 0):
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.best_price_seen": current_price,
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": f"New best price: ${current_price}"
                        }}
                    )
                else:
                    # Just update last checked timestamp
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": f"Current price: ${current_price}"
                        }}
                    )
                
                # Check if price condition is met
                if current_price >= target_price_usd:
                    logger.info(f"Limit order {order_id} conditions met! Current price: ${current_price}, Target: ${target_price_usd}")
                    
                    # Get the appropriate tool for execution
                    tool = self._get_tool_for_content(order.get('content_type'))
                    if not tool:
                        logger.error(f"No tool found for content type: {order.get('content_type')}")
                        return
                    
                    # Execute using the tool's execute_scheduled_operation method
                    try:
                        # Ensure numeric values are strings for the NEAR API
                        order['content']['operation_details']['from_amount'] = str(from_amount)
                        result = await tool.execute_scheduled_operation(order)
                        logger.info(f"Execution result: {result}")
                        
                        if result.get('success'):
                            await self.db.tool_items.update_one(
                                {"_id": ObjectId(order_id)},
                                {"$set": {
                                    "status": OperationStatus.EXECUTED.value,
                                    "state": ToolOperationState.COMPLETED.value,
                                    "executed_time": datetime.now(UTC),
                                    "api_response": result,
                                    "metadata.execution_result": result,
                                    "metadata.execution_completed_at": datetime.now(UTC).isoformat()
                                }}
                            )
                    except Exception as exec_error:
                        logger.error(f"Error executing limit order {order_id}: {exec_error}")
                        await self.db.tool_items.update_one(
                            {"_id": ObjectId(order_id)},
                            {"$set": {
                                "status": OperationStatus.FAILED.value,
                                "metadata.execution_error": str(exec_error),
                                "metadata.execution_error_time": datetime.now(UTC).isoformat()
                            }}
                        )
                
            except Exception as e:
                logger.error(f"Error checking price for limit order {order_id}: {e}")
                
                # Update with error
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "parameters.custom_params.last_checked_timestamp": int(time.time()),
                        "metadata.last_error": f"Error checking price: {str(e)}",
                        "metadata.last_error_time": datetime.now(UTC).isoformat(),
                        "metadata.error_count": order.get("metadata", {}).get("error_count", 0) + 1
                    }}
                )
            
        except Exception as e:
            logger.error(f"Error in _check_limit_order: {e}", exc_info=True)

    async def _execute_limit_order(self, order):
        """Signal schedule manager to execute the limit order"""
        try:
            order_id = str(order.get('_id'))
            content = order.get("content", {})
            
            # Update order status to indicate price conditions met
            await self.db.tool_items.update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {
                    "status": OperationStatus.EXECUTING.value,
                    "metadata.execution_started_at": datetime.now(UTC).isoformat()
                }}
            )
            
            # Signal schedule manager to execute the operation
            await self.schedule_manager.execute_scheduled_operation(order)
            
        except Exception as e:
            logger.error(f"Error executing limit order: {e}", exc_info=True)

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

    async def _check_price_with_coingecko(self, from_token: str, to_token: str) -> Optional[float]:
        try:
            # Get prices for both tokens
            from_price = await self.coingecko_client.get_token_price(from_token)
            to_price = await self.coingecko_client.get_token_price(to_token)
            
            if from_price and to_price:
                # Calculate relative price
                relative_price = to_price["price_usd"] / from_price["price_usd"]
                return relative_price
            return None
        except Exception as e:
            logger.error(f"Error checking CoinGecko price: {e}")
            return None