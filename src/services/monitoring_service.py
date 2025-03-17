import asyncio
import logging
from datetime import datetime, UTC, timedelta
import time
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from typing import Dict, List, Optional, Any
import json

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
                current_time = datetime.now(UTC)
                
                # Update query to match our actual data structure
                active_orders = await self.db.tool_items.find({
                    "content_type": ContentType.LIMIT_ORDER.value,
                    "metadata.scheduling_type": "monitored",
                    "state": ToolOperationState.COMPLETED.value,
                    "status": OperationStatus.SCHEDULED.value
                }).to_list(None)
                
                logger.info(f"Monitoring service checking {len(active_orders)} active limit orders at {current_time.isoformat()}")
                
                if active_orders:
                    for order in active_orders:
                        try:
                            # Get operation details from the correct location
                            operation_details = self._get_operation_details(order)
                            
                            # Extract monitoring parameters
                            token_to_monitor = operation_details.get("reference_token")
                            target_price_usd = float(operation_details.get("target_price_usd", 0))
                            
                            logger.info(f"Processing limit order {order['_id']}: "
                                      f"Monitoring {token_to_monitor} for target price ${target_price_usd}")
                            
                            if token_to_monitor and target_price_usd > 0:
                                await self._check_limit_order(order)
                            else:
                                logger.error(f"Invalid monitoring parameters for order {order['_id']}: "
                                           f"token={token_to_monitor}, target=${target_price_usd}")
                            
                        except Exception as e:
                            logger.error(f"Error checking limit order {order.get('_id')}: {e}", exc_info=True)
                else:
                    logger.debug("No active limit orders found for monitoring")
                
                await asyncio.sleep(self._check_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    def _get_operation_details(self, order: Dict) -> Dict:
        """Extract operation details from any item structure"""
        try:
            # For debugging
            order_id = str(order.get('_id', 'unknown'))
            logger.info(f"Getting operation details for {order_id}")
            
            # Check custom_params first (most reliable source)
            custom_params = order.get("parameters", {}).get("custom_params", {})
            if custom_params and custom_params.get("reference_token") and custom_params.get("target_price_usd"):
                logger.info(f"Found reliable params in custom_params for {order_id}")
                return {
                    "from_token": custom_params.get("from_token"),
                    "from_amount": str(custom_params.get("from_amount")),
                    "to_token": custom_params.get("to_token"),
                    "target_price_usd": float(custom_params.get("target_price_usd")),
                    "reference_token": custom_params.get("reference_token"),
                    "to_chain": custom_params.get("to_chain", "ethereum")  # Default to ethereum
                }
            
            # Then check content paths
            for path in [
                ["content", "operation_details"],
                ["content", "raw_content", "operation_details"],
                ["operation_details"]
            ]:
                obj = order
                for key in path:
                    if not isinstance(obj, dict):
                        obj = {}
                        break
                    obj = obj.get(key, {})
                
                if obj and obj.get("reference_token") and obj.get("target_price_usd"):
                    logger.info(f"Found valid operation details in {'.'.join(path)} for {order_id}")
                    return {
                        "from_token": obj.get("from_token"),
                        "from_amount": str(obj.get("from_amount", "")),
                        "to_token": obj.get("to_token"),
                        "target_price_usd": float(obj.get("target_price_usd", 0)),
                        "reference_token": obj.get("reference_token"),
                        "to_chain": obj.get("to_chain", "ethereum")  # Default to ethereum
                    }
            
            # Last attempt: log the structure and return an empty dict
            logger.error(f"Could not find valid monitoring parameters for {order_id}:")
            logger.error(f"Item structure preview: {json.dumps({k: v for k, v in order.items() if k not in ['content']}, indent=2)}")
            logger.error(f"Content structure: {json.dumps(order.get('content', {}), indent=2)}")
            logger.error(f"Parameters structure: {json.dumps(order.get('parameters', {}), indent=2)}")
            
            return {}
            
        except Exception as e:
            logger.error(f"Error extracting operation details: {str(e)}", exc_info=True)
            return {}

    async def _check_limit_order(self, order):
        """Check if a limit order's conditions are met using CoinGecko USD prices"""
        try:
            order_id = str(order.get('_id'))
            operation_details = self._get_operation_details(order)
            
            # Log full details for debugging
            logger.info(f"Processing order {order_id} with operation details: {operation_details}")
            
            # Get the reference token and target price
            token_to_monitor = operation_details.get("reference_token")
            target_price_usd = float(operation_details.get("target_price_usd", 0))
            
            # Validate required parameters
            if not token_to_monitor or target_price_usd <= 0:
                logger.error(f"Missing required parameters for limit order {order_id}: "
                            f"token_to_monitor={token_to_monitor}, target_price_usd={target_price_usd}")
                logger.error(f"Full operation details: {operation_details}")
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(order_id)},
                    {"$set": {
                        "parameters.custom_params.last_checked_timestamp": int(time.time()),
                        "metadata.last_error": f"Missing required parameters: token_to_monitor={token_to_monitor}, target_price_usd={target_price_usd}",
                        "metadata.last_error_time": datetime.now(UTC).isoformat(),
                        "metadata.error_details": {
                            "operation_details": operation_details,
                            "item_structure": order
                        }
                    }}
                )
                return

            # Keep expiration check
            expiration_timestamp = order.get("parameters", {}).get("custom_params", {}).get("expiration_timestamp")
            if expiration_timestamp and time.time() > expiration_timestamp:
                await self._expire_limit_order(order)
                return
            
            # Get current USD price from CoinGecko
            try:
                coingecko_id = await self.coingecko_client._get_coingecko_id(token_to_monitor)
                if not coingecko_id:
                    logger.error(f"Could not find CoinGecko ID for {token_to_monitor}")
                    return
                
                price_data = await self.coingecko_client.get_token_price(coingecko_id)
                if not price_data or 'price_usd' not in price_data:
                    logger.error(f"Could not get price data for {token_to_monitor}")
                    return
                
                current_price = float(price_data['price_usd'])
                
                logger.info(f"Current {token_to_monitor} price: ${current_price}, Target: ${target_price_usd}")
                
                # Update best price seen and timestamp
                if current_price > order.get("parameters", {}).get("custom_params", {}).get("best_price_seen", 0):
                    await self.db.tool_items.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {
                            "parameters.custom_params.best_price_seen": current_price,
                            "parameters.custom_params.last_checked_timestamp": int(time.time()),
                            "metadata.last_check_result": f"New best price: ${current_price}",
                            "metadata.best_price_seen": current_price,
                            "metadata.last_check_time": datetime.now(UTC).isoformat()
                        }}
                    )
                else:
                    # Update timestamp only
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
                    
                    try:
                        # Ensure numeric values are strings for the NEAR API
                        from_amount = operation_details.get("from_amount")
                        if isinstance(from_amount, (int, float)):
                            from_amount = str(from_amount)
                        
                        # Create execution copy with proper structure
                        execution_order = {
                            **order,
                            "content": {
                                "operation_details": {
                                    **operation_details,
                                    "from_amount": from_amount
                                }
                            }
                        }
                        
                        # Execute the order
                        result = await tool.execute_scheduled_operation(execution_order)
                        logger.info(f"Execution result for {order_id}: {result}")
                        
                        if result.get('success'):
                            await self.db.tool_items.update_one(
                                {"_id": ObjectId(order_id)},
                                {"$set": {
                                    "status": OperationStatus.EXECUTED.value,
                                    "state": ToolOperationState.COMPLETED.value,
                                    "executed_time": datetime.now(UTC),
                                    "api_response": result,
                                    "metadata.execution_result": result,
                                    "metadata.execution_price": current_price,
                                    "metadata.execution_time": datetime.now(UTC).isoformat(),
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
                logger.error(f"Error checking price for {token_to_monitor}: {e}")
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
                    "metadata.best_price_seen": operation_details.get("best_price_seen", 0)
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
                "state": ToolOperationState.COMPLETED.value
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