import pytest
import asyncio
import logging
import sys
import os
from datetime import datetime, UTC, timedelta
from bson.objectid import ObjectId
import json
from dotenv import load_dotenv
import argparse
from typing import Dict

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-8s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Add src directory to path - match test_tweet_scheduling.py exactly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Import with src. prefix like test_tweet_scheduling.py
from src.db.enums import ToolOperationState, OperationStatus, ScheduleState, ContentType, ToolType
from src.db.mongo_manager import MongoManager
from src.managers.tool_state_manager import ToolStateManager
from src.managers.schedule_manager import ScheduleManager
from src.managers.approval_manager import ApprovalManager
from src.services.schedule_service import ScheduleService
from src.services.llm_service import LLMService, ModelType
from src.tools.orchestrator import Orchestrator
from src.clients.near_intents_client.intents_client import (
    intent_swap,
    get_intent_balance,
    create_token_diff_quote,
    publish_intent,
    IntentRequest,
    fetch_options,
    select_best_option,
    smart_withdraw
)
from src.tools.intents_operation import IntentsTool
from src.clients.coingecko_client import CoinGeckoClient

# Load environment variables
load_dotenv()

# Add this before the IntentsLimitOrderTester class
class TestIntentsTool(IntentsTool):
    """Test implementation of IntentsTool with improved error handling"""
    
    def can_handle(self, command_text, tool_type=None):
        """Check if this tool can handle the given command"""
        # Simple implementation for testing
        limit_order_keywords = ["limit order", "price", "when price", "sell", "buy"]
        return any(keyword in command_text.lower() for keyword in limit_order_keywords)
    
    async def _generate_content(self, topic: str, count: int, schedule_id: str = None, tool_operation_id: str = None) -> Dict:
        """Generate human-readable content for limit order approval with better error handling"""
        try:
            logger.info(f"Generating limit order content for approval: {topic}")
            
            # Get parent operation to access stored parameters
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
            
            # Get the parameters from _analyze_command
            params = operation.get("input_data", {}).get("command_info", {}).get("parameters", {})
            logger.info(f"Using parameters from operation: {params}")
            
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

            logger.info(f"Sending content generation prompt to LLM")
            
            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.5,
                    "max_tokens": 500
                }
            )
            
            # Log raw response
            logger.info(f"Raw LLM response for content generation: {response}")
            
            # If response is empty or not valid JSON, use a fallback
            try:
                generated_content = json.loads(response)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse LLM response as JSON: {e}")
                # Provide fallback content
                generated_content = {
                    "title": f"Limit Order: {params['from_token']} to {params['to_token']} at ${params['target_price_usd']}",
                    "description": f"This limit order will sell {params['from_amount']} {params['from_token']} for {params['to_token']} when the price reaches ${params['target_price_usd']} per {params['from_token']}.",
                    "warnings": ["Market prices are volatile", "Order may expire before execution if price target isn't met"],
                    "expected_outcome": f"When {params['from_token']} reaches ${params['target_price_usd']}, you will receive {params['to_token']} based on current market rates."
                }
                logger.info(f"Using fallback content: {generated_content}")
            
            # Create tool item for approval
            tool_item = {
                "session_id": self.deps.session_id,
                "tool_operation_id": tool_operation_id,
                "schedule_id": schedule_id,
                "content_type": self.registry.content_type.value,
                "state": operation["state"],
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
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value,
                        }
                    }
                }
            )

            logger.info(f"Successfully created content item with ID: {item_id}")
            return {
                "items": [tool_item],
                "schedule_id": schedule_id,
                "tool_operation_id": tool_operation_id
            }

        except Exception as e:
            logger.error(f"Error generating limit order content: {e}", exc_info=True)
            raise

class IntentsLimitOrderTester:
    """Test the complete limit order flow including monitoring and execution"""
    
    def __init__(self, mongo_uri="mongodb://localhost:27017"):
        self.mongo_uri = mongo_uri
        self.session_id = f"test_session_{int(datetime.now(UTC).timestamp())}"
        self.db = None
        self.tool_state_manager = None
        self.schedule_manager = None
        self.approval_manager = None
        self.orchestrator = None
        self.schedule_service = None
        self.coingecko_client = None
        self.llm_service = None
        self.operation_id = None
        self.schedule_id = None

    async def setup(self):
        """Initialize all required components"""
        # Initialize MongoDB first and store the instance
        await MongoManager.initialize(self.mongo_uri)
        self.db = MongoManager.get_db()
        MongoManager._db = self.db  # Ensure the class variable is set
        
        # Initialize managers and services in order
        self.tool_state_manager = ToolStateManager(db=self.db)
        self.coingecko_client = CoinGeckoClient(api_key=os.getenv('COINGECKO_API_KEY'))
        self.llm_service = LLMService({"model_type": ModelType.GROQ_LLAMA_3_3_70B})
        
        # Initialize schedule manager with empty tool registry
        self.schedule_manager = ScheduleManager(
            tool_state_manager=self.tool_state_manager,
            db=self.db,
            tool_registry={}
        )
        
        # Initialize approval manager
        self.approval_manager = ApprovalManager(
            tool_state_manager=self.tool_state_manager,
            db=self.db,
            llm_service=self.llm_service,
            schedule_manager=self.schedule_manager
        )
        
        # Initialize orchestrator after DB is ready
        self.orchestrator = Orchestrator()
        self.approval_manager.orchestrator = self.orchestrator
        
        # Initialize schedule service
        self.schedule_service = ScheduleService(
            mongo_uri=self.mongo_uri,
            orchestrator=self.orchestrator
        )
        self.orchestrator.set_schedule_service(self.schedule_service)
        
        # Initialize Intents tool
        self.intents_tool = TestIntentsTool(deps=self.orchestrator.deps)
        self.intents_tool.inject_dependencies(
            tool_state_manager=self.tool_state_manager,
            llm_service=self.llm_service,
            approval_manager=self.approval_manager,
            schedule_manager=self.schedule_manager,
            coingecko_client=self.coingecko_client
        )
        
        # Register tool with schedule manager
        self.schedule_manager.tool_registry[ContentType.LIMIT_ORDER.value] = self.intents_tool
        
        # Start schedule service
        await self.schedule_service.start()
        
        logger.info("Test environment setup complete")

    async def teardown(self):
        """Clean up resources"""
        if hasattr(self, 'operation_id'):
            await self.db.tool_operations.delete_one({"_id": ObjectId(self.operation_id)})
            await self.db.tool_items.delete_many({"tool_operation_id": self.operation_id})
        if hasattr(self, 'schedule_id'):
            await self.db.scheduled_operations.delete_one({"_id": ObjectId(self.schedule_id)})
        await self.schedule_service.stop()
        logger.info("Test environment teardown complete")

    async def test_complete_flow(self):
        """Test the complete flow from creation to monitoring"""
        try:
            # Setup test environment
            await self.setup()
            
            # Run test_limit_order_creation
            logger.info("Testing limit order creation...")
            operation = await self.test_limit_order_creation()
            logger.info(f"Created test operation with ID: {self.operation_id}")
            
            # Run test_price_monitoring
            logger.info("\nTesting price monitoring...")
            await self.test_price_monitoring()
            
            logger.info("All tests completed successfully!")
            return True
            
        except Exception as e:
            logger.error(f"Test failed: {e}")
            import traceback
            traceback.print_exc()
            return False
            
        finally:
            # Clean up
            await self.teardown()
    
    @pytest.mark.asyncio
    async def test_limit_order_creation(self):
        """Test creating a limit order operation"""
        try:
            # Create operation
            operation = await self.tool_state_manager.start_operation(
                session_id=self.session_id,
                tool_type=ToolType.INTENTS.value,
                initial_data={
                    "command": "create limit order to sell 0.1 NEAR when price reaches $3.00",
                    "tool_type": ToolType.INTENTS.value
                }
            )
            
            self.operation_id = str(operation["_id"])
            
            # Verify operation created correctly
            assert operation is not None
            assert operation["state"] == ToolOperationState.COLLECTING.value
            assert operation["tool_type"] == ToolType.INTENTS.value
            
            # Set session_id in tool's dependencies
            self.intents_tool.deps.session_id = self.session_id
            
            # Test command analysis
            command_result = await self.intents_tool._analyze_command(
                "create limit order to sell 0.1 NEAR when price reaches $3.00"
            )
            
            # Verify command analysis
            assert command_result is not None
            assert "schedule_id" in command_result
            assert "parameters" in command_result
            assert command_result["parameters"]["price_oracle"]["symbol"] == "NEAR"
            assert command_result["parameters"]["price_oracle"]["target_price_usd"] == 3.0
            assert command_result["parameters"]["swap"]["from_amount"] == 0.1
            
            self.schedule_id = command_result["schedule_id"]
            
            # Test content generation
            content_result = await self.intents_tool._generate_content(
                topic=command_result["topic"],
                count=1,
                schedule_id=self.schedule_id,
                tool_operation_id=self.operation_id
            )
            
            # Verify content generation
            assert content_result is not None
            assert "items" in content_result
            assert len(content_result["items"]) == 1
            assert "content" in content_result["items"][0]
            
            logger.info("✅ Limit order creation test passed")
            
        except Exception as e:
            logger.error(f"❌ Test failed: {e}")
            raise

    @pytest.mark.asyncio
    async def test_price_monitoring(self):
        """Test price monitoring functionality"""
        try:
            # Get current NEAR price
            near_price = await self.coingecko_client.get_token_price("near")
            current_price = near_price["price_usd"]
            
            # Create limit order slightly above current price
            target_price = current_price * 1.001
            command = f"create limit order to sell 0.1 NEAR when price reaches ${target_price:.2f}"
            
            # Create and analyze operation
            operation = await self.tool_state_manager.start_operation(
                session_id=self.session_id,
                tool_type=ToolType.INTENTS.value,
                initial_data={"command": command}
            )
            
            self.operation_id = str(operation["_id"])
            command_result = await self.intents_tool._analyze_command(command)
            self.schedule_id = command_result["schedule_id"]
        
        # Verify monitoring parameters
            assert "monitoring_params" in command_result
            assert command_result["monitoring_params"]["check_interval_seconds"] > 0
            
            # Test price check
            current_price = (await self.coingecko_client.get_token_price("near"))["price_usd"]
            logger.info(f"Current NEAR price: ${current_price}, Target: ${target_price}")
            
            assert current_price > 0, "Failed to get valid price from CoinGecko"
            
            logger.info("✅ Price monitoring test passed")
            
        except Exception as e:
            logger.error(f"❌ Test failed: {e}")
            raise

async def main():
    """Run the limit order execution test"""
    parser = argparse.ArgumentParser(description='Test limit order flow')
    parser.add_argument('--execute-real', action='store_true', help='Execute real transactions')
    args = parser.parse_args()

    tester = IntentsLimitOrderTester()
    success = await tester.test_complete_flow()
    
    if success:
        logger.info("✅ Limit order flow test passed!")
    else:
        logger.error("❌ Limit order flow test failed!")
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    asyncio.run(main())