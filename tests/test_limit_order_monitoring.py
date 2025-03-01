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
        
        # Initialize Solver Bus client
        self.solver_bus_client = SolverBusClient(SOLVER_BUS_URL)
        
        # Test parameters
        self.from_token = "NEAR"
        self.to_token = "USDC"
        self.from_amount = 0.1  # Small amount for testing
        self.min_price = 0  # Set to 0 to always execute for testing
        self.check_interval = 10  # Check every 10 seconds
        
    async def create_test_limit_order(self):
        """Create a test limit order in the database"""
        await self.db.initialize()
        
        # Create a test tool item for a limit order
        order_data = {
            "session_id": "test_session",
            "tool_operation_id": str(ObjectId()),
            "content_type": "limit_order",
            "state": ToolOperationState.EXECUTING.value,
            "status": OperationStatus.PENDING.value,
            "content": {
                "operation_type": "limit_order",
                "from_token": self.from_token,
                "from_amount": self.from_amount,
                "to_token": self.to_token,
                "to_chain": "near",
                "min_price": self.min_price,
                "raw_content": f"Limit order: {self.from_amount} {self.from_token} to {self.to_token} at min price {self.min_price}",
                "formatted_content": f"Limit order: {self.from_amount} {self.from_token} to {self.to_token} at min price {self.min_price}",
                "version": "1.0"
            },
            "parameters": {
                "custom_params": {}
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
        
    async def test_manual_quote_monitoring(self):
        """Test manual monitoring of quotes for a limit order"""
        # Create a test limit order
        order_id = await self.create_test_limit_order()
        
        # Set up monitoring parameters
        monitoring_params = {
            "check_interval_seconds": self.check_interval,
            "expiration_seconds": 300  # 5 minutes for testing
        }
        
        # Initialize monitoring service
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            solver_bus_client=self.solver_bus_client
        )
        
        # Register the limit order with the monitoring service
        registration_result = await monitoring_service.register_limit_order(order_id, monitoring_params)
        assert registration_result, "Failed to register limit order"
        
        # Manually check quotes for 5 iterations
        logger.info("Starting manual quote monitoring test")
        
        for i in range(5):
            logger.info(f"Iteration {i+1}/5")
            
            # Get the order from the database
            order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
            assert order, f"Order {order_id} not found"
            
            # Log current order status
            logger.info(f"Order status: {order.get('status')}")
            logger.info(f"Best price seen: {order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
            
            # Get quotes manually
            try:
                # Convert amount to proper decimal format
                from_token_info = get_token_by_symbol(self.from_token)
                from_decimals = from_token_info.get('decimals', 24) if from_token_info else 24
                decimal_amount = str(int(self.from_amount * 10**from_decimals))
                
                # Get asset IDs
                from_asset_id = to_asset_id(self.from_token)
                to_asset_id = to_asset_id(self.to_token)
                
                # Get quotes from Solver Bus
                quote_result = await self.solver_bus_client.get_quote(
                    token_in=from_asset_id,
                    token_out=to_asset_id,
                    amount_in=decimal_amount
                )
                
                if quote_result.get("success", False):
                    solver_quotes = quote_result.get("quotes", [])
                    
                    if solver_quotes:
                        # Find best quote
                        best_option = None
                        best_amount_out = 0
                        
                        for quote in solver_quotes:
                            if 'amount_out' in quote:
                                amount_out = float(quote['amount_out'])
                                if amount_out > best_amount_out:
                                    best_amount_out = amount_out
                                    best_option = quote
                        
                        if best_option:
                            # Calculate current price
                            to_token_info = get_token_by_symbol(self.to_token)
                            to_decimals = to_token_info.get('decimals', 6) if to_token_info else 6
                            
                            human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals)
                            human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals)
                            
                            current_price = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                            
                            logger.info(f"Current price: {current_price} {self.to_token}/{self.from_token}")
                            logger.info(f"You would receive: {human_amount_out} {self.to_token} for {human_amount_in} {self.from_token}")
                            
                            # Print quote details
                            logger.info(f"Quote hash: {best_option.get('quote_hash')}")
                            logger.info(f"Solver ID: {best_option.get('solver_id')}")
                    else:
                        logger.warning("No quotes available")
                else:
                    logger.error(f"Failed to get quotes: {quote_result.get('error')}")
                    
            except Exception as e:
                logger.error(f"Error getting quotes: {e}")
            
            # Wait for next check
            await asyncio.sleep(self.check_interval)
            
        # Check final order status
        order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        logger.info(f"Final order status: {order.get('status')}")
        logger.info(f"Best price seen: {order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
        
        # Clean up
        await self.db.tool_items.delete_one({"_id": ObjectId(order_id)})
        logger.info(f"Deleted test order {order_id}")
        
    async def test_monitoring_service(self):
        """Test the full monitoring service functionality"""
        # Create a test limit order
        order_id = await self.create_test_limit_order()
        
        # Set up monitoring parameters
        monitoring_params = {
            "check_interval_seconds": self.check_interval,
            "expiration_seconds": 300  # 5 minutes for testing
        }
        
        # Initialize and start monitoring service
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            solver_bus_client=self.solver_bus_client
        )
        
        # Register the limit order with the monitoring service
        registration_result = await monitoring_service.register_limit_order(order_id, monitoring_params)
        assert registration_result, "Failed to register limit order"
        
        # Start the monitoring service
        await monitoring_service.start()
        logger.info("Monitoring service started")
        
        # Let the service run for a while
        logger.info("Letting monitoring service run for 60 seconds...")
        await asyncio.sleep(60)
        
        # Stop the monitoring service
        await monitoring_service.stop()
        logger.info("Monitoring service stopped")
        
        # Check final order status
        order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        logger.info(f"Final order status: {order.get('status')}")
        logger.info(f"Best price seen: {order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
        
        # If the order was executed, show the execution result
        if order.get('status') == OperationStatus.EXECUTED.value:
            logger.info(f"Order was executed!")
            logger.info(f"Execution result: {order.get('metadata', {}).get('execution_result')}")
        
        # Clean up
        await self.db.tool_items.delete_one({"_id": ObjectId(order_id)})
        logger.info(f"Deleted test order {order_id}")

    async def test_direct_quote_fetching(self):
        """Test direct quote fetching without using the monitoring service"""
        logger.info("Testing direct quote fetching")
        
        # Convert amount to proper decimal format
        from_token_info = get_token_by_symbol(self.from_token)
        from_decimals_val = from_token_info.get('decimals', 24) if from_token_info else 24
        decimal_amount = str(int(self.from_amount * 10**from_decimals_val))
        
        # Get asset IDs
        from_asset_id_val = to_asset_id(self.from_token)
        to_asset_id_val = to_asset_id(self.to_token)
        
        # Fetch quotes 5 times with a delay
        for i in range(5):
            logger.info(f"Quote fetch iteration {i+1}/5")
            
            try:
                # Get quotes from Solver Bus
                quote_result = await self.solver_bus_client.get_quote(
                    token_in=from_asset_id_val,
                    token_out=to_asset_id_val,
                    amount_in=decimal_amount
                )
                
                logger.info(f"Quote result success: {quote_result.get('success', False)}")
                
                if quote_result.get("success", False):
                    solver_quotes = quote_result.get("quotes", [])
                    logger.info(f"Received {len(solver_quotes)} quotes")
                    
                    if solver_quotes:
                        # Find best quote
                        best_option = None
                        best_amount_out = 0
                        
                        for quote in solver_quotes:
                            if 'amount_out' in quote:
                                amount_out = float(quote['amount_out'])
                                if amount_out > best_amount_out:
                                    best_amount_out = amount_out
                                    best_option = quote
                        
                        if best_option:
                            # Calculate current price
                            to_token_info = get_token_by_symbol(self.to_token)
                            to_decimals_val = to_token_info.get('decimals', 6) if to_token_info else 6
                            
                            human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals_val)
                            human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals_val)
                            
                            current_price = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                            
                            logger.info(f"Best quote details:")
                            logger.info(f"Current price: {current_price} {self.to_token}/{self.from_token}")
                            logger.info(f"You would receive: {human_amount_out} {self.to_token} for {human_amount_in} {self.from_token}")
                            logger.info(f"Quote hash: {best_option.get('quote_hash')}")
                            logger.info(f"Solver ID: {best_option.get('solver_id')}")
                            
                            # Save the quote to a file for reference
                            with open(f"quote_result_{i}.json", "w") as f:
                                json.dump(best_option, f, indent=2)
                                logger.info(f"Saved quote to quote_result_{i}.json")
                    else:
                        logger.warning("No quotes available")
                else:
                    logger.error(f"Failed to get quotes: {quote_result.get('error')}")
                    
            except Exception as e:
                logger.error(f"Error getting quotes: {e}")
            
            # Wait before next fetch
            await asyncio.sleep(self.check_interval)

    async def test_execution_trigger(self):
        """Test execution trigger logic with a low min_price to ensure it executes"""
        logger.info("Testing execution trigger")
        
        # Set up monitoring parameters
        monitoring_params = {
            "check_interval_seconds": self.check_interval,
            "expiration_seconds": 300  # 5 minutes for testing
        }
        
        # Initialize and start monitoring service
        monitoring_service = LimitOrderMonitoringService(MONGO_URI)
        await monitoring_service.inject_dependencies(
            near_account=self.near_account,
            solver_bus_client=self.solver_bus_client
        )
        
        # Register the limit order with the monitoring service
        order_id = await self.create_test_limit_order()
        registration_result = await monitoring_service.register_limit_order(order_id, monitoring_params)
        assert registration_result, "Failed to register limit order"
        
        # Start the monitoring service
        await monitoring_service.start()
        logger.info("Monitoring service started")
        
        # Let the service run for a while
        logger.info("Letting monitoring service run for 60 seconds...")
        await asyncio.sleep(60)
        
        # Stop the monitoring service
        await monitoring_service.stop()
        logger.info("Monitoring service stopped")
        
        # Check final order status
        order = await self.db.tool_items.find_one({"_id": ObjectId(order_id)})
        logger.info(f"Final order status: {order.get('status')}")
        logger.info(f"Best price seen: {order.get('parameters', {}).get('custom_params', {}).get('best_price_seen', 0)}")
        
        # If the order was executed, show the execution result
        if order.get('status') == OperationStatus.EXECUTED.value:
            logger.info(f"Order was executed!")
            logger.info(f"Execution result: {order.get('metadata', {}).get('execution_result')}")
        else:
            logger.warning(f"Order was not executed. Status: {order.get('status')}")
        
        # Clean up
        await self.db.tool_items.delete_one({"_id": ObjectId(order_id)})
        logger.info(f"Deleted test order {order_id}")

    async def test_create_token_diff_quote(self):
        """Test creating a token diff quote for execution"""
        logger.info("Testing create_token_diff_quote function")
        
        try:
            # Get the best quote first
            request = IntentRequest()
            request.asset_in(self.from_token, self.from_amount)
            request.asset_out(self.to_token, chain="eth")
            
            # Get quotes
            solver_quotes = fetch_options(request)
            
            if not solver_quotes:
                logger.error("No quotes available for testing create_token_diff_quote")
                return
                
            # Find best quote
            best_option = select_best_option(solver_quotes)
            
            if not best_option:
                logger.error("No best option found")
                return
                
            # Get token info
            from_token_info = get_token_by_symbol(self.from_token)
            to_token_info = get_token_by_symbol(self.to_token)
            
            from_decimals_val = from_token_info.get('decimals', 24) if from_token_info else 24
            to_decimals_val = to_token_info.get('decimals', 6) if to_token_info else 6
            
            # Calculate human-readable amounts
            human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals_val)
            human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals_val)
            
            # Create token diff quote
            logger.info(f"Creating token diff quote for {human_amount_in} {self.from_token} to {human_amount_out} {self.to_token}")
            
            # Get asset IDs
            from_asset_id = to_asset_id(self.from_token)
            to_asset_id_val = to_asset_id(self.to_token)
            
            # Create the quote
            quote = await create_token_diff_quote(
                account=self.near_account,
                token_in=self.from_token,
                amount_in=human_amount_in,
                token_out=self.to_token,
                amount_out=human_amount_out,
                from_asset_id=from_asset_id,
                to_asset_id=to_asset_id_val
            )
            
            logger.info(f"Created token diff quote: {quote}")
            logger.info(f"Quote signature: {quote.get('signature')}")
            
            # Save the quote to a file for reference
            with open("token_diff_quote.json", "w") as f:
                json.dump(quote, f, indent=2)
                logger.info("Saved token diff quote to token_diff_quote.json")
                
            # Test publishing the intent (commented out to avoid actual execution)
            # logger.info("Publishing intent...")
            # publish_result = await publish_intent(self.near_account, quote)
            # logger.info(f"Publish result: {publish_result}")
            
        except Exception as e:
            logger.error(f"Error creating token diff quote: {e}", exc_info=True)

    async def test_intents_tool_limit_order_creation(self):
        """Test limit order creation through IntentsTool"""
        from tools.intents_operation import IntentsTool
        from services.llm_service import LLMService, ModelType
        from managers.tool_state_manager import ToolStateManager
        from managers.schedule_manager import ScheduleManager
        
        # Initialize dependencies
        await self.db.initialize()
        tool_state_manager = ToolStateManager(db=self.db)
        
        # Create a mock LLM service that returns a predefined response
        class MockLLMService:
            async def get_response(self, prompt, model_type=None, override_config=None):
                return """
                {
                    "tools_needed": [{
                        "tool_name": "intents",
                        "action": "limit_order",
                        "parameters": {
                            "from_token": "NEAR",
                            "from_amount": 0.1,
                            "to_token": "USDC",
                            "min_price": 1.5,
                            "to_chain": "eth",
                            "expiration_hours": 24,
                            "slippage": 0.5
                        },
                        "priority": 1
                    }],
                    "reasoning": "User requested a limit order to swap NEAR to USDC when the price reaches a specific threshold"
                }
                """
        
        # Create a mock schedule manager
        class MockScheduleManager:
            async def initialize_schedule(self, tool_operation_id, schedule_info, content_type, session_id=None):
                logger.info(f"Initializing schedule for operation {tool_operation_id}")
                logger.info(f"Schedule info: {schedule_info}")
                return "mock_schedule_id"
        
        # Initialize IntentsTool
        intents_tool = IntentsTool()
        intents_tool.tool_state_manager = tool_state_manager
        intents_tool.llm_service = MockLLMService()
        intents_tool.schedule_manager = MockScheduleManager()
        intents_tool.near_account = self.near_account
        intents_tool.solver_bus_client = self.solver_bus_client
        intents_tool.db = self.db
        
        # Create a test session and operation
        session_id = "test_session_intents_tool"
        operation_id = await tool_state_manager.create_operation(
            session_id=session_id,
            tool_name="intents",
            command="I want to swap 0.1 NEAR for USDC at $1.50 / NEAR"
        )
        
        # Set the session ID for the tool
        intents_tool.deps = type('obj', (object,), {'session_id': session_id})
        
        # Analyze the command
        command = "I want to swap 0.1 NEAR for USDC at $1.50 / NEAR"
        result = await intents_tool._analyze_command(command)
        
        # Verify the result
        assert result["operation_type"] == "limit_order"
        assert result["parameters"]["from_token"] == "NEAR"
        assert result["parameters"]["from_amount"] == 0.1
        assert result["parameters"]["to_token"] == "USDC"
        assert result["parameters"]["min_price"] == 1.5
        assert "monitoring_params" in result
        assert "schedule_id" in result
        
        # Now test the _generate_content method
        content_result = await intents_tool._generate_content(result)
        
        # Verify content generation
        assert content_result["success"] is True
        assert "items" in content_result
        assert len(content_result["items"]) > 0
        assert content_result["operation_type"] == "limit_order"
        
        # Clean up
        await self.db.tool_operations.delete_one({"_id": ObjectId(operation_id)})
        logger.info(f"Deleted test operation {operation_id}")

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
    
    # Choose which test to run
    # await test.test_manual_quote_monitoring()
    # await test.test_monitoring_service()
    await test.test_direct_quote_fetching()  # Uncomment to run this test
    # await test.test_execution_trigger()
    # await test.test_create_token_diff_quote()
    await test.test_intents_tool_limit_order_creation()  # Uncomment to run this test

if __name__ == "__main__":
    asyncio.run(run_tests()) 