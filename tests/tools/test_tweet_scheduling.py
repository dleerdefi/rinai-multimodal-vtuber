import pytest
import os
import sys
from pathlib import Path
from datetime import datetime, UTC, timedelta
import logging
import asyncio
from bson.objectid import ObjectId

# Add project root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

# Load environment variables before imports
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(project_root) / '.env')

# Now import project modules
from src.tools.post_tweets import TwitterTool
from src.managers.schedule_manager import ScheduleManager
from src.managers.tool_state_manager import ToolStateManager
from src.managers.approval_manager import ApprovalManager
from src.services.llm_service import LLMService
from src.tools.base import AgentDependencies
from src.db.mongo_manager import MongoManager
from src.db.enums import ToolOperationState, OperationStatus, ScheduleState, ContentType

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

class MockTwitterClient:
    async def send_tweet(self, content: str, params: dict) -> dict:
        logger.info(f"Mock sending tweet: {content}")
        return {
            'success': True,
            'id': '123456789',
            'text': content
        }

class MockApprovalHandlers:
    def get(self, action):
        """Return appropriate handler based on action"""
        handlers = {
            'approve': self.handle_approval,
            'reject': self.handle_rejection,
            'full_approval': self.handle_approval,
            'partial_approval': self.handle_approval,
            'regenerate': self.handle_rejection
        }
        return handlers.get(action)

    async def handle_approval(self, tool_operation_id=None, session_id=None, analysis=None, regenerate_count=None):
        """Mock approval handler with complete signature"""
        # Get the operation and items from the database
        db = MongoManager.get_db()
        
        # First update all items to EXECUTING state and APPROVED status
        await db.tool_items.update_many(
            {"tool_operation_id": tool_operation_id},
            {"$set": {
                "state": ToolOperationState.EXECUTING.value,
                "status": OperationStatus.APPROVED.value,
                "metadata.approval_state": "approval_finished",
                "last_updated": datetime.now(UTC)
            }}
        )
        
        # Update operation state to EXECUTING
        await db.tool_operations.update_one(
            {"_id": ObjectId(tool_operation_id)},
            {"$set": {
                "state": ToolOperationState.EXECUTING.value,
                "metadata.approval_state": "approval_finished",
                "last_updated": datetime.now(UTC)
            }}
        )
        
        return {
            "success": True,
            "message": "Items approved successfully",
            "operation_state": ToolOperationState.EXECUTING.value,
            "items_status": OperationStatus.APPROVED.value,
            "regenerate_count": regenerate_count or 0
        }
    
    async def handle_rejection(self, tool_operation_id=None, session_id=None, analysis=None):
        """Mock rejection handler with complete signature"""
        return {
            "success": False,
            "message": "Items rejected",
            "state": ToolOperationState.REJECTED.value,
            "status": OperationStatus.REJECTED.value
        }

@pytest.fixture(autouse=True)
async def setup_teardown():
    """Setup and teardown for all tests"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    
    yield
    
    # Teardown
    try:
        db = MongoManager.get_db()
        await db.scheduled_operations.delete_many({})
        await db.tool_operations.delete_many({})
        await db.tool_items.delete_many({})
    finally:
        await MongoManager.close()

@pytest.mark.asyncio
async def test_tweet_scheduling_workflow():
    """Test complete tweet scheduling workflow including generation, approval, and scheduling"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup
        deps = AgentDependencies(
            session_id="test_schedule_session",
            user_id="test_user",
            context={},
            tools_available=["twitter"]
        )
        
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        schedule_manager = ScheduleManager(
            tool_state_manager=tool_state_manager,
            db=db,
            tool_registry={}
        )
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service,
            schedule_manager=schedule_manager
        )
        
        tweet_tool = TwitterTool(
            deps=deps,
            tool_state_manager=tool_state_manager,
            llm_service=llm_service,
            approval_manager=approval_manager,
            schedule_manager=schedule_manager
        )
        tweet_tool.twitter_client = MockTwitterClient()
        
        # Register tool with schedule manager
        schedule_manager.tool_registry[ContentType.TWEET.value] = tweet_tool
        
        # 1. Test Command Analysis
        command = "Schedule 3 tweets about AI spread over the next 24 hours"
        analysis_result = await tweet_tool._analyze_twitter_command(command)
        
        assert analysis_result["topic"] in ["AI", "artificial intelligence"]
        assert analysis_result["item_count"] == 3
        assert "tool_operation_id" in analysis_result
        assert "schedule_id" in analysis_result
        
        # Get operation and verify schedule info
        operation = await tool_state_manager.get_operation_by_id(analysis_result["tool_operation_id"])
        assert operation is not None
        assert operation["metadata"]["requires_scheduling"] is True
        assert "schedule_info" in operation["metadata"]
        schedule_info = operation["metadata"]["schedule_info"]
        assert schedule_info["schedule_type"] == "one_time"
        assert schedule_info["schedule_time"] == "spread_24h"
        assert schedule_info["interval_minutes"] == 480  # 24 hours / 3 tweets
        
        # 2. Test Tweet Generation
        generation_result = await tweet_tool._generate_tweets(
            topic=analysis_result["topic"],
            count=analysis_result["item_count"],
            schedule_id=analysis_result["schedule_id"],
            tool_operation_id=analysis_result["tool_operation_id"]
        )
        
        assert len(generation_result["items"]) == 3
        for item in generation_result["items"]:
            assert "content" in item
            assert len(item["content"]["raw_content"]) <= 280
        
        # 3. Test Approval Flow
        approval_result = await approval_manager.start_approval_flow(
            session_id=deps.session_id,
            tool_operation_id=analysis_result["tool_operation_id"],
            items=generation_result["items"]
        )
        
        # Simulate approval with handlers
        handlers = MockApprovalHandlers()
        approval_response = await approval_manager.process_approval_response(
            message="approve all",
            session_id=deps.session_id,
            content_type=ContentType.TWEET.value,
            tool_operation_id=analysis_result["tool_operation_id"],
            handlers=handlers
        )
        
        # Wait a moment for state changes to propagate
        await asyncio.sleep(0.1)
        
        # Verify both operation and item states
        operation = await tool_state_manager.get_operation_by_id(analysis_result["tool_operation_id"])
        assert operation["state"] == ToolOperationState.EXECUTING.value

        # Check items too
        items = await db.tool_items.find({
            "tool_operation_id": analysis_result["tool_operation_id"]
        }).to_list(None)

        for item in items:
            assert item["state"] == ToolOperationState.EXECUTING.value
            assert item["status"] == OperationStatus.APPROVED.value

        # Verify schedule state after initialization
        schedule = await db.get_scheduled_operation(analysis_result["schedule_id"])
        assert schedule["schedule_state"] == ScheduleState.PENDING.value
        
        # After approval, activate schedule
        schedule_result = await schedule_manager.activate_schedule(
            tool_operation_id=analysis_result["tool_operation_id"],
            schedule_info=operation["metadata"]["schedule_info"],
            content_type=ContentType.TWEET
        )
        assert schedule_result is True

        # Verify schedule transitions through states
        schedule = await db.get_scheduled_operation(analysis_result["schedule_id"])
        assert schedule["schedule_state"] == ScheduleState.ACTIVE.value
        assert len(schedule["state_history"]) >= 3  # PENDING -> ACTIVATING -> ACTIVE
        
        # Verify state history contains correct transitions
        state_history = schedule["state_history"]
        assert state_history[0]["state"] == ScheduleState.PENDING.value
        assert state_history[1]["state"] == ScheduleState.ACTIVATING.value
        assert state_history[2]["state"] == ScheduleState.ACTIVE.value

        # Test pause/resume functionality
        await schedule_manager.pause_schedule(analysis_result["schedule_id"])
        schedule = await db.get_scheduled_operation(analysis_result["schedule_id"])
        assert schedule["schedule_state"] == ScheduleState.PAUSED.value

        await schedule_manager.resume_schedule(analysis_result["schedule_id"])
        schedule = await db.get_scheduled_operation(analysis_result["schedule_id"])
        assert schedule["schedule_state"] == ScheduleState.ACTIVE.value

        # Verify scheduled items
        scheduled_items = await db.tool_items.find({
            "tool_operation_id": analysis_result["tool_operation_id"]
        }).to_list(None)
        
        assert len(scheduled_items) == 3
        for item in scheduled_items:
            assert item["status"] == OperationStatus.SCHEDULED.value
            assert "scheduled_time" in item["metadata"]
            scheduled_time = datetime.fromisoformat(item["metadata"]["scheduled_time"].replace('Z', '+00:00'))
            assert scheduled_time > datetime.now(UTC)

        # Test schedule cancellation
        await schedule_manager.cancel_schedule(analysis_result["schedule_id"])
        schedule = await db.get_scheduled_operation(analysis_result["schedule_id"])
        assert schedule["schedule_state"] == ScheduleState.CANCELLED.value

        logger.info("Tweet scheduling flow test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error: {e}")
        raise
        
    finally:
        # Cleanup
        await db.scheduled_operations.delete_many({"session_id": deps.session_id})
        await db.tool_operations.delete_many({"session_id": deps.session_id})
        await db.tool_items.delete_many({"session_id": deps.session_id})
        await MongoManager.close()

if __name__ == "__main__":
    pytest.main(["-v", "test_tweet_scheduling.py"]) 