import asyncio
import logging
import os
import sys
import time
from datetime import datetime, UTC, timedelta
import json
from dotenv import load_dotenv
from bson.objectid import ObjectId
from near_api.account import Account
from near_api.signer import KeyPair, Signer
from near_api.providers import JsonProvider

# Add the src directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from clients.near_intents_client.intents_client import (
    intent_swap,
    get_intent_balance,
    create_token_diff_quote,
    publish_intent,
    IntentRequest,
    fetch_options,
    select_best_option
)
from clients.near_intents_client.config import (
    get_token_by_symbol,
    to_asset_id,
    to_decimals,
    from_decimals
)
from clients.solver_bus_client import SolverBusClient
from services.monitoring_service import LimitOrderMonitoringService
from db.db_schema import RinDB
from db.enums import OperationStatus, ToolOperationState
from motor.motor_asyncio import AsyncIOMotorClient
from clients.coingecko_client import CoinGeckoClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
SOLVER_BUS_URL = os.getenv("SOLVER_BUS_URL", "https://solver-bus.near.org")

class TestLimitOrderMonitoring:
    """Test class for limit order monitoring functionality"""
    
    def setup_method(self):
        """Set up test environment before each test"""
        # Initialize MongoDB client
        self.mongo_client = AsyncIOMotorClient(MONGO_URI)
        self.db = RinDB(self.mongo_client)
        
        # Initialize NEAR account using environment variables
        account_id = os.getenv('NEAR_ACCOUNT_ID')
        private_key = os.getenv('NEAR_PRIVATE_KEY')
        rpc_url = os.getenv('NEAR_RPC_URL', 'https://rpc.mainnet.near.org')
        
        # Import necessary NEAR API classes
        from near_api.account import Account
        from near_api.signer import KeyPair, Signer
        from near_api.providers import JsonProvider
        
        # Create NEAR account
        provider = JsonProvider(rpc_url)
        key_pair = KeyPair(private_key)
        signer = Signer(account_id, key_pair)
        self.near_account = Account(provider, signer, account_id)
        
        # Initialize CoinGecko client with API key from environment
        coingecko_api_key = os.getenv('COINGECKO_API_KEY')
        if not coingecko_api_key:
            raise ValueError("COINGECKO_API_KEY environment variable is not set")
        self.coingecko_client = CoinGeckoClient(api_key=coingecko_api_key)
        
        # Test parameters
        self.from_token = "NEAR"
        self.to_token = "USDC"
        self.from_amount = 0.1
        self.target_price_usd = 3.0  # Target price in USD
        self.check_interval = 10
        
    async def create_test_limit_order(self):
        """Create a test limit order in the database"""
        await self.db.initialize()
        
        order_data = {
            "session_id": "test_session",
            "tool_operation_id": str(ObjectId()),
            "content_type": "limit_order",
            "state": ToolOperationState.EXECUTING.value,
            "status": OperationStatus.SCHEDULED.value,
            "content": {
                "operation_type": "limit_order",
                "from_token": self.from_token,
                "from_amount": self.from_amount,
                "to_token": self.to_token,
                "target_price_usd": self.target_price_usd,
                "raw_content": f"Limit order: {self.from_amount} {self.from_token} when price reaches ${self.target_price_usd}"
            },
            "parameters": {
                "custom_params": {
                    "check_interval_seconds": self.check_interval,
                    "last_checked_timestamp": 0,
                    "best_price_seen": 0,
                    "expiration_timestamp": int((datetime.now(UTC) + timedelta(hours=24)).timestamp())
                }
            },
            "metadata": {
                "created_at": datetime.now(UTC).isoformat(),
                "source": "test_script"
            }
        }
        
        result = await self.db.tool_items.insert_one(order_data)
        order_id = str(result.inserted_id)
        logger.info(f"Created test limit order with ID: {order_id}")
        
        return order_id

    async def test_price_monitoring(self):
        """Test price monitoring using real CoinGecko prices"""
        # Create a test limit order
        order_id = await self.create_test_limit_order()
        
        # Initialize monitoring service with real CoinGecko client
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            coingecko_client=self.coingecko_client  # Using real CoinGecko client
        )
        
        logger.info("\nStarting price monitoring test with real CoinGecko data")
        logger.info(f"Monitoring {self.from_token} price, target: ${self.target_price_usd}")
        
        # Test for 5 iterations
        for i in range(5):
            logger.info(f"\nPrice check iteration {i+1}/5")
            
            try:
                # Get current market price from CoinGecko
                coingecko_id = await self.coingecko_client._get_coingecko_id(self.from_token)
                if not coingecko_id:
                    logger.error(f"Could not find CoinGecko ID for {self.from_token}")
                    continue
                
                price_data = await self.coingecko_client.get_token_price(coingecko_id)
                current_price = price_data.get('price_usd') if price_data else None
                
                if current_price:
                    logger.info(f"Current {self.from_token} price: ${current_price}")
                    logger.info(f"Target price: ${self.target_price_usd}")
                else:
                    logger.warning(f"Could not get current price for {self.from_token}")
                    continue
                
                # Get the order and check price conditions
                order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
                await monitoring_service._check_limit_order(order)
                
                # Get updated order and log status
                updated_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
                best_price = updated_order.get("parameters", {}).get("custom_params", {}).get("best_price_seen", 0)
                last_check = updated_order.get("metadata", {}).get("last_check_result", "No check result")
                
                logger.info(f"Best price seen: ${best_price}")
                logger.info(f"Last check result: {last_check}")
                
                # If price target was met, log the event
                if current_price >= self.target_price_usd:
                    logger.info(f"🎯 Price target met! Current price (${current_price}) >= Target (${self.target_price_usd})")
                    if updated_order.get("status") == OperationStatus.EXECUTED.value:
                        logger.info("Order execution was triggered!")
                
            except Exception as e:
                logger.error(f"Error in monitoring iteration: {e}")
            
            # Wait before next check
            await asyncio.sleep(self.check_interval)
            
        # Final order status
        final_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        logger.info("\nFinal order status:")
        logger.info(f"Status: {final_order.get('status')}")
        logger.info(f"State: {final_order.get('state')}")
        logger.info(f"Best price seen: ${final_order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
        
        # Clean up
        await self.db.tool_items.delete_one({"_id": ObjectId(order_id)})
        logger.info(f"\nTest completed and cleaned up order {order_id}")

    async def test_execution_trigger(self):
        """Test execution trigger when price target is met"""
        # Create test order with low target price to ensure trigger
        self.target_price_usd = 0.1  # Set very low to ensure trigger
        order_id = await self.create_test_limit_order()
        
        # Track execution calls
        execution_called = False
        
        class MockScheduleManager:
            def __init__(self, db: RinDB):
                self.db = db

            async def execute_scheduled_operation(self, operation):
                nonlocal execution_called
                execution_called = True
                logger.info(f"Mock executing operation: {operation['_id']}")
                
                # Update the operation status and state
                await self.db.tool_items.update_one(
                    {"_id": operation["_id"]},
                    {"$set": {
                        "status": OperationStatus.EXECUTED.value,
                        "state": ToolOperationState.COMPLETED.value,
                        "executed_time": datetime.now(UTC),
                        "metadata.execution_completed_at": datetime.now(UTC).isoformat()
                    }}
                )
                
                return {"success": True}
        
        # Initialize service with proper mock
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            coingecko_client=self.coingecko_client,
            schedule_manager=MockScheduleManager(self.db)  # Pass db to mock
        )
        
        # Start monitoring
        await monitoring_service.start()
        
        # Wait for potential execution
        timeout = 60
        start_time = time.time()
        
        while not execution_called and (time.time() - start_time) < timeout:
            await asyncio.sleep(5)
            
        # Stop monitoring
        await monitoring_service.stop()
        
        # Verify execution was triggered
        assert execution_called, "Execution was not triggered within timeout"
        
        # Allow time for state updates to complete
        await asyncio.sleep(1)
        
        # Check final order status
        final_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        assert final_order["status"] == OperationStatus.EXECUTED.value
        assert final_order["state"] == ToolOperationState.COMPLETED.value

    async def test_price_monitoring_with_expiration(self):
        """Test price monitoring with order expiration"""
        # Create order that expires quickly
        self.target_price_usd = 1000000  # Set very high to ensure no trigger
        order_id = await self.create_test_limit_order()
        
        # Update expiration to 5 seconds from now
        await self.db.tool_items.update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {
                "parameters.custom_params.expiration_timestamp": int(time.time() + 5)
            }}
        )
        
        # Initialize service
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            coingecko_client=self.coingecko_client,
            schedule_manager=None  # No need for schedule manager in this test
        )
        
        # Wait for expiration
        await asyncio.sleep(6)
        
        # Check order one more time
        order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        await monitoring_service._check_limit_order(order)
        
        # Verify order was expired
        final_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        assert final_order["status"] == OperationStatus.FAILED.value
        assert "expired_at" in final_order["metadata"]

    async def test_live_price_tracking(self):
        """Test continuous price tracking with real CoinGecko data"""
        logger.info("\nStarting live price tracking test")
        
        # Get initial price to set a realistic target
        coingecko_id = await self.coingecko_client._get_coingecko_id(self.from_token)
        initial_price = (await self.coingecko_client.get_token_price(coingecko_id))['price_usd']
        
        # Set target price slightly above current price
        self.target_price_usd = initial_price * 1.001  # 0.1% above current price
        
        logger.info(f"Initial {self.from_token} price: ${initial_price}")
        logger.info(f"Target price set to: ${self.target_price_usd}")
        
        # Create test order
        order_id = await self.create_test_limit_order()
        
        # Initialize monitoring service
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            coingecko_client=self.coingecko_client
        )
        
        # Track execution trigger
        execution_triggered = False
        start_time = time.time()
        timeout = 300  # 5 minutes timeout
        
        while not execution_triggered and (time.time() - start_time) < timeout:
            try:
                # Get current price
                price_data = await self.coingecko_client.get_token_price(coingecko_id)
                current_price = price_data.get('price_usd')
                
                if current_price:
                    logger.info(f"\nCurrent {self.from_token} price: ${current_price}")
                    logger.info(f"Target price: ${self.target_price_usd}")
                    logger.info(f"Price difference: {((current_price - self.target_price_usd) / self.target_price_usd) * 100:.4f}%")
                
                # Check order
                order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
                await monitoring_service._check_limit_order(order)
                
                # Check if execution was triggered
                updated_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
                if updated_order.get("status") == OperationStatus.EXECUTED.value:
                    execution_triggered = True
                    logger.info("\n🎯 Execution triggered!")
                    break
                
            except Exception as e:
                logger.error(f"Error in live tracking: {e}")
            
            await asyncio.sleep(self.check_interval)
        
        # Final status
        final_order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        logger.info("\nFinal tracking results:")
        logger.info(f"Execution triggered: {execution_triggered}")
        logger.info(f"Time elapsed: {time.time() - start_time:.2f} seconds")
        logger.info(f"Final status: {final_order.get('status')}")
        logger.info(f"Best price seen: ${final_order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
        
        # Clean up
        await self.db.tool_items.delete_one({"_id": ObjectId(order_id)})
        logger.info(f"\nTest completed and cleaned up order {order_id}")

    async def teardown_method(self):
        """Clean up resources after each test"""
        # Close MongoDB connection
        self.mongo_client.close()
        
        # Any other cleanup needed
        logger.info("Test cleanup completed")

# Run the tests
async def run_tests():
    test = TestLimitOrderMonitoring()
    test.setup_method()
    
    logger.info("Running price monitoring test...")
    await test.test_price_monitoring()
    
    logger.info("\nRunning live price tracking test...")
    await test.test_live_price_tracking()
    
    await test.teardown_method()

if __name__ == "__main__":
    asyncio.run(run_tests()) 