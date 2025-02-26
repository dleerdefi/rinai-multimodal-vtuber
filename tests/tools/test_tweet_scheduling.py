import asyncio
import logging
import sys
import os
from datetime import datetime, UTC, timedelta
from bson.objectid import ObjectId
import json
import argparse

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-8s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Add src directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Import required modules
from src.db.mongo_manager import MongoManager
from src.managers.tool_state_manager import ToolStateManager
from src.managers.schedule_manager import ScheduleManager
from src.managers.approval_manager import ApprovalManager
from src.services.schedule_service import ScheduleService
from src.services.llm_service import LLMService, ModelType
from src.tools.orchestrator import Orchestrator
from src.db.enums import ToolOperationState, OperationStatus, ScheduleState, ContentType, ToolType
from src.clients.twitter_client import TwitterAgentClient
from src.tools.post_tweets import TwitterTool

class TwitterScheduleExecutionTester:
    """Test the complete Twitter scheduling and execution flow"""
    
    def __init__(self, mongo_uri="mongodb://localhost:27017", post_real_tweets=False):
        self.mongo_uri = mongo_uri
        self.session_id = f"test_session_{int(datetime.now(UTC).timestamp())}"
        self.db = None
        self.tool_state_manager = None
        self.schedule_manager = None
        self.approval_manager = None
        self.orchestrator = None
        self.schedule_service = None
        self.twitter_client = None
        self.llm_service = None
        self.operation_id = None
        self.schedule_id = None
        self.post_real_tweets = post_real_tweets
        
    async def setup(self):
        """Initialize all required components"""
        logger.info("Setting up test environment...")
        
        # Initialize MongoDB
        await MongoManager.initialize(self.mongo_uri)
        self.db = MongoManager.get_db()
        
        # Initialize managers and services
        self.tool_state_manager = ToolStateManager(db=self.db)
        self.twitter_client = TwitterAgentClient()
        self.llm_service = LLMService({"model_type": ModelType.GROQ_LLAMA_3_3_70B})
        
        # Initialize schedule manager with empty tool registry (will be populated later)
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
        
        # Initialize orchestrator
        self.orchestrator = Orchestrator()
        
        # Set orchestrator in approval manager (needed for regeneration)
        self.approval_manager.orchestrator = self.orchestrator
        
        # Initialize schedule service
        self.schedule_service = ScheduleService(
            mongo_uri=self.mongo_uri,
            orchestrator=self.orchestrator
        )
        
        # Set schedule service in orchestrator
        self.orchestrator.set_schedule_service(self.schedule_service)
        
        # Initialize Twitter tool
        self.twitter_tool = TwitterTool(deps=self.orchestrator.deps)
        self.twitter_tool.inject_dependencies(
            tool_state_manager=self.tool_state_manager,
            llm_service=self.llm_service,
            approval_manager=self.approval_manager,
            schedule_manager=self.schedule_manager
        )
        
        # Register Twitter tool with schedule manager
        self.schedule_manager.tool_registry[ContentType.TWEET.value] = self.twitter_tool
        
        # Start schedule service
        await self.schedule_service.start()
        
        logger.info("Test environment setup complete")
    
    async def teardown(self):
        """Clean up resources"""
        logger.info("Tearing down test environment...")
        
        # Stop schedule service
        if self.schedule_service:
            await self.schedule_service.stop()
        
        # Clean up test data
        if self.db and self.operation_id:
            await self.db.tool_operations.delete_one({"_id": ObjectId(self.operation_id)})
            await self.db.tool_items.delete_many({"tool_operation_id": self.operation_id})
            if self.schedule_id:
                await self.db.scheduled_operations.delete_one({"_id": ObjectId(self.schedule_id)})
        
        logger.info("Test environment teardown complete")
    
    async def create_test_operation(self):
        """Create a test tool operation for scheduling tweets"""
        logger.info("Creating test tool operation...")
        
        # Create operation
        operation = await self.tool_state_manager.start_operation(
            session_id=self.session_id,
            tool_type=ToolType.TWITTER.value,
            initial_data={
                "command": "schedule 2 tweets about AI technology",
                "tool_type": ToolType.TWITTER.value
            }
        )
        
        self.operation_id = str(operation["_id"])
        logger.info(f"Created test operation with ID: {self.operation_id}")
        
        # Set the session_id in the Twitter tool's dependencies
        self.twitter_tool.deps.session_id = self.session_id
        
        # Create schedule directly (like in test_schedule_flow.py)
        schedule_info = {
            "start_time": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            "interval_minutes": 5,
            "schedule_type": "one_time",
            "total_items": 2
        }
        
        self.schedule_id = await self.schedule_manager.initialize_schedule(
            tool_operation_id=self.operation_id,
            schedule_info=schedule_info,
            content_type=ContentType.TWEET.value,
            session_id=self.session_id
        )
        
        logger.info(f"Created test schedule with ID: {self.schedule_id}")
        
        # Update operation with schedule info
        await self.tool_state_manager.update_operation(
            session_id=self.session_id,
            tool_operation_id=self.operation_id,
            metadata={
                "schedule_id": self.schedule_id,
                "content_type": ContentType.TWEET.value,
                "requires_scheduling": True,
                "requires_approval": True,
                "schedule_state": ScheduleState.PENDING.value
            },
            input_data={
                "command_info": {
                    "topic": "AI technology",
                    "item_count": 2,
                    "schedule_info": schedule_info
                },
                "schedule_id": self.schedule_id
            }
        )
        
        # Generate tweet content
        generation_result = await self.twitter_tool._generate_content(
            topic="AI technology",
            count=2,
            schedule_id=self.schedule_id,
            tool_operation_id=self.operation_id
        )
        
        logger.info(f"Generated {len(generation_result['items'])} test tweets")
        
        # Update operation state to COLLECTING
        await self.tool_state_manager.update_operation(
            session_id=self.session_id,
            tool_operation_id=self.operation_id,
            state=ToolOperationState.COLLECTING.value
        )
        
        return operation, generation_result["items"]
    
    async def test_approval_flow(self):
        """Test the approval flow"""
        logger.info("Testing approval flow...")
        
        # Get items
        items = await self.tool_state_manager.get_operation_items(
            tool_operation_id=self.operation_id,
            state=ToolOperationState.COLLECTING.value
        )
        
        # Start approval flow
        await self.tool_state_manager.update_operation(
            session_id=self.session_id,
            tool_operation_id=self.operation_id,
            state=ToolOperationState.APPROVING.value
        )
        
        approval_result = await self.approval_manager.start_approval_flow(
            session_id=self.session_id,
            tool_operation_id=self.operation_id,
            items=items
        )
        
        logger.info(f"Approval flow started: {approval_result.get('approval_state')}")
        
        # Simulate full approval
        current_items = await self.tool_state_manager.get_operation_items(
            tool_operation_id=self.operation_id,
            state=ToolOperationState.APPROVING.value
        )
        
        full_approval = await self.approval_manager._handle_full_approval(
            tool_operation_id=self.operation_id,
            session_id=self.session_id,
            items=current_items,
            analysis={"action": "full_approval", "approved_indices": list(range(len(current_items)))}
        )
        
        logger.info(f"Full approval result: {full_approval.get('state')}/{full_approval.get('status')}")
        
        # Verify items are in EXECUTING/APPROVED state
        executing_items = await self.tool_state_manager.get_operation_items(
            tool_operation_id=self.operation_id,
            state=ToolOperationState.EXECUTING.value,
            status=OperationStatus.APPROVED.value
        )
        
        logger.info(f"Found {len(executing_items)} items in EXECUTING/APPROVED state")
        assert len(executing_items) == 2, "Expected 2 items in EXECUTING/APPROVED state"
        
        return full_approval
    
    async def test_schedule_activation(self):
        """Test schedule activation"""
        logger.info("Testing schedule activation...")
        
        # Activate schedule
        activation_result = await self.schedule_manager.activate_schedule(
            tool_operation_id=self.operation_id,
            schedule_id=self.schedule_id
        )
        
        logger.info(f"Schedule activation result: {activation_result}")
        assert activation_result is True, "Schedule activation failed"
        
        # Verify items are in COMPLETED/SCHEDULED state
        scheduled_items = await self.tool_state_manager.get_operation_items(
            tool_operation_id=self.operation_id,
            status=OperationStatus.SCHEDULED.value
        )
        
        logger.info(f"Found {len(scheduled_items)} items in SCHEDULED status")
        assert len(scheduled_items) == 2, "Expected 2 items in SCHEDULED status"
        
        # Verify schedule is in ACTIVE state
        schedule = await self.db.get_scheduled_operation(self.schedule_id)
        logger.info(f"Schedule state: {schedule.get('schedule_state')}")
        assert schedule.get('schedule_state') == ScheduleState.ACTIVE.value, "Expected schedule to be in ACTIVE state"
        
        # Update operation to COMPLETED state
        await self.tool_state_manager.end_operation(
            session_id=self.session_id,
            success=True,
            api_response={"message": "Schedule activated successfully"}
        )
        
        # Verify operation is in COMPLETED state
        operation = await self.tool_state_manager.get_operation_by_id(self.operation_id)
        logger.info(f"Operation state after end_operation: {operation.get('state')}/{operation.get('status')}")
        assert operation.get('state') == ToolOperationState.COMPLETED.value, "Expected operation to be in COMPLETED state"
        
        return schedule, scheduled_items
    
    async def test_tweet_execution(self):
        """Test execution of schedule"""
        logger.info("Testing tweet execution...")
        
        # Get scheduled items
        scheduled_items = await self.tool_state_manager.get_operation_items(
            tool_operation_id=self.operation_id,
            status=OperationStatus.SCHEDULED.value
        )
        
        # Execute each tweet
        executed_items = []
        for item in scheduled_items:
            logger.info(f"Executing tweet: {item.get('content', {}).get('raw_content')}")
            
            # Execute tweet using Twitter client with test_mode parameter
            execution_result = await self.twitter_client.send_tweet(
                content=item.get('content', {}).get('raw_content'),
                params={
                    'account_id': 'default',
                    'media_files': []
                },
                test_mode=not self.post_real_tweets  # Use test mode unless post_real_tweets is True
            )
            
            logger.info(f"Tweet execution result: {execution_result}")
            
            # Update item status to EXECUTED
            await self.schedule_manager.update_item_execution_status(
                item_id=str(item.get('_id')),
                status=OperationStatus.EXECUTED,
                api_response=execution_result
            )
            
            executed_items.append(item)
            
            # Verify item is now in COMPLETED/EXECUTED state
            updated_item = await self.db.tool_items.find_one({"_id": item.get('_id')})
            logger.info(f"Item state after execution: {updated_item.get('state')}/{updated_item.get('status')}")
            assert updated_item.get('state') == ToolOperationState.COMPLETED.value, "Expected item to be in COMPLETED state"
            assert updated_item.get('status') == OperationStatus.EXECUTED.value, "Expected item to have EXECUTED status"
            
            if self.post_real_tweets and execution_result.get("success"):
                # Verify the tweet was posted by checking the tweet ID
                tweet_id = execution_result.get("id")
                if tweet_id:
                    logger.info(f"Successfully posted tweet with ID: {tweet_id}")
                    # You could add additional verification here, like fetching the tweet
                else:
                    logger.warning("Tweet posted but no ID returned")
        
        # Check if schedule is now COMPLETED
        await self.schedule_manager.check_schedule_completion(self.schedule_id)
        schedule = await self.db.get_scheduled_operation(self.schedule_id)
        logger.info(f"Schedule state after execution: {schedule.get('schedule_state')}")
        
        return executed_items
    
    async def test_complete_flow(self):
        """Test the complete flow from approval to execution"""
        try:
            # Setup test environment
            await self.setup()
            
            # Create test operation and items
            operation, items = await self.create_test_operation()
            logger.info(f"Created test operation with {len(items)} items")
            
            # Test approval flow
            approval_result = await self.test_approval_flow()
            logger.info(f"Approval flow completed with status: {approval_result.get('status')}")
            
            # Test schedule activation
            schedule, scheduled_items = await self.test_schedule_activation()
            logger.info(f"Schedule activation completed with state: {schedule.get('schedule_state')}")
            
            # Test actual tweet execution
            executed_items = await self.test_tweet_execution()
            logger.info(f"Tweet execution completed with {len(executed_items)} executed items")
            
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

async def main():
    """Run the Twitter schedule execution test"""
    parser = argparse.ArgumentParser(description='Test Twitter scheduling and execution')
    parser.add_argument('--post-real-tweets', action='store_true', help='Post real tweets to Twitter')
    args = parser.parse_args()

    tester = TwitterScheduleExecutionTester(post_real_tweets=args.post_real_tweets)
    success = await tester.test_complete_flow()
    
    if success:
        logger.info("✅ Twitter schedule execution test passed!")
    else:
        logger.error("❌ Twitter schedule execution test failed!")
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    asyncio.run(main())