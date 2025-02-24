import pytest
import os
import sys
from pathlib import Path
from datetime import datetime, UTC, timedelta
import logging
import asyncio
from bson.objectid import ObjectId
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from motor.motor_asyncio import AsyncIOMotorClient

# Add project root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

# Load environment variables before imports
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(project_root) / '.env')

# Now import project modules
from src.tools.post_tweets import TwitterTool
from src.managers.schedule_manager import ScheduleManager, ScheduleAction
from src.managers.tool_state_manager import ToolStateManager
from src.managers.approval_manager import ApprovalManager
from src.services.llm_service import LLMService
from src.tools.base import AgentDependencies
from src.db.mongo_manager import MongoManager
from src.db.enums import ToolOperationState, OperationStatus, ScheduleState, ContentType, ToolType
from src.db.db_schema import RinDB
from src.services.schedule_service import ScheduleService

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

class MockCollection:
    """Mock MongoDB collection with async methods"""
    def __init__(self):
        self.find_one = AsyncMock()
        self.find_one_and_update = AsyncMock()
        self.update_one = AsyncMock()
        self.update_many = AsyncMock()
        self.insert_one = AsyncMock()
        self.delete_many = AsyncMock()
        self.find = AsyncMock()
        
        # Fix for to_list attribute error
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        self.find.return_value = mock_cursor

class MockDB(RinDB):
    """Mock RinDB that inherits from actual RinDB"""
    def __init__(self):
        self.tool_operations = MockCollection()
        self.tool_items = MockCollection()
        self.scheduled_operations = MockCollection()
        
    async def initialize(self):
        """Mock initialize method"""
        pass
    
    async def get_scheduled_operation(self, schedule_id: str = None, **kwargs) -> dict:
        """Override to use mocked collection"""
        if schedule_id:
            return await self.scheduled_operations.find_one({"_id": ObjectId(schedule_id)})
        return await self.scheduled_operations.find_one(kwargs)

class MockTwitterClient:
    async def send_tweet(self, content: str, params: dict) -> dict:
        logger.info(f"Mock sending tweet: {content}")
        return {
            'success': True,
            'id': '123456789',
            'text': content
        }
    
    async def execute_scheduled_operation(self, operation: dict) -> dict:
        return {
            'success': True,
            'id': '123456789',
            'text': operation.get('content', {}).get('raw_content', '')
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
                "state": ToolOperationState.COMPLETED.value,
                "metadata.approval_state": "approval_finished",
                "last_updated": datetime.now(UTC)
            }}
        )
        
        return {
            "success": True,
            "message": "Items approved successfully",
            "state": ToolOperationState.COMPLETED.value,
            "status": OperationStatus.SCHEDULED.value,
            "regenerate_count": regenerate_count or 0
        }
    
    async def handle_rejection(self, tool_operation_id=None, session_id=None, analysis=None):
        """Mock rejection handler with complete signature"""
        return {
            "success": False,
            "message": "Items rejected",
            "state": ToolOperationState.CANCELLED.value,
            "status": OperationStatus.REJECTED.value
        }

@pytest.fixture(autouse=True)
async def setup_teardown():
    """Setup and teardown for all tests"""
    yield
    
    # Teardown
    try:
        db = MongoManager.get_db()
        await db.scheduled_operations.delete_many({})
        await db.tool_operations.delete_many({})
        await db.tool_items.delete_many({})
    finally:
        await MongoManager.close()

@pytest.fixture
def mock_db():
    """Create mock database"""
    return MockDB()

@pytest.fixture
def tool_state_manager(mock_db):
    """Create ToolStateManager instance"""
    return ToolStateManager(db=mock_db)

@pytest.fixture
def schedule_manager(mock_db, tool_state_manager):
    """Create ScheduleManager instance"""
    return ScheduleManager(
        tool_state_manager=tool_state_manager,
        db=mock_db,
        tool_registry={'twitter': MockTwitterClient()}
    )

@pytest.fixture
async def schedule_service(mock_db, tool_state_manager, schedule_manager):
    """Create ScheduleService instance with mocked components"""
    service = ScheduleService("mock_uri")
    service.db = mock_db
    service.tool_state_manager = tool_state_manager
    service._tools = {
        'twitter': MockTwitterClient()
    }
    
    await service.start()
    yield service
    await service.stop()

@pytest.mark.asyncio
async def test_tweet_scheduling_workflow(mock_db, schedule_manager):
    """Test complete tweet scheduling workflow"""
    operation_id = ObjectId()
    schedule_id = ObjectId()
    
    # Mock initial operation creation
    initial_operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.EXECUTING.value,
        "tool_type": ToolType.TWITTER.value,
        "content_type": ContentType.TWEET.value,
        "metadata": {
            "requires_scheduling": True,
            "content_type": ContentType.TWEET.value,
            "schedule_info": {
                "schedule_type": "one_time",
                "schedule_time": "spread_24h",
                "interval_minutes": 480
            },
            "expected_item_count": 3  # Add expected count
        }
    }
    
    # Create and schedule items
    items = [
        {
            "_id": ObjectId(),
            "content": {"raw_content": f"Tweet {i}"},
            "tool_operation_id": str(operation_id),
            "state": ToolOperationState.EXECUTING.value,
            "status": OperationStatus.APPROVED.value,
            "metadata": {
                "content_type": ContentType.TWEET.value,
                "requires_scheduling": True
            }
        } for i in range(3)
    ]
    
    # Mock tool state manager methods
    schedule_manager.tool_state_manager.get_operation_items = AsyncMock(return_value=items)
    schedule_manager.tool_state_manager.get_operation_by_id = AsyncMock(return_value=initial_operation)
    schedule_manager.tool_state_manager.update_operation_state = AsyncMock(return_value=True)
    
    # Mock database operations
    mock_db.tool_operations.find_one = AsyncMock(return_value=initial_operation)
    mock_items_cursor = AsyncMock()
    mock_items_cursor.to_list = AsyncMock(return_value=items)
    mock_db.tool_items.find = AsyncMock(return_value=mock_items_cursor)
    
    # Mock schedule operations
    mock_db.scheduled_operations.insert_one = AsyncMock(return_value=AsyncMock(inserted_id=schedule_id))
    mock_db.scheduled_operations.find_one = AsyncMock(return_value={
        "_id": schedule_id,
        "schedule_state": ScheduleState.PENDING.value,
        "tool_operation_id": str(operation_id)
    })
    
    # Mock update operations
    mock_db.scheduled_operations.update_one = AsyncMock(return_value=AsyncMock(modified_count=1))
    mock_db.tool_items.update_one = AsyncMock(return_value=AsyncMock(modified_count=1))
    mock_db.tool_items.update_many = AsyncMock(return_value=AsyncMock(modified_count=len(items)))
    mock_db.tool_operations.update_one = AsyncMock(return_value=AsyncMock(modified_count=1))
    
    # Schedule approved items
    result = await schedule_manager.schedule_approved_items(
        tool_operation_id=str(operation_id),
        schedule_info={
            "schedule_type": "one_time",
            "schedule_time": "spread_24h",
            "interval_minutes": 480
        }
    )
    assert result is True

@pytest.mark.asyncio
async def test_schedule_error_handling(mock_db, schedule_manager):
    """Test error handling in schedule operations"""
    schedule_id = ObjectId()
    
    # Mock database error
    mock_db.scheduled_operations.find_one = AsyncMock(return_value={
        "_id": schedule_id,
        "schedule_state": ScheduleState.PENDING.value,
        "tool_operation_id": str(ObjectId())
    })
    
    # Mock the update_schedule_state method to raise an error
    mock_db.update_schedule_state = AsyncMock(side_effect=Exception("Database error"))
    
    # The method should return False when there's an error, not raise an exception
    result = await schedule_manager._transition_schedule_state(
        schedule_id=str(schedule_id),
        action=ScheduleAction.ACTIVATE,
        reason="Test activation"
    )
    
    # Check that the method returned False
    assert result is False
    
    # Verify that the error was logged
    # Note: We could add a mock logger and verify the exact error message if needed

@pytest.mark.asyncio
async def test_invalid_schedule_transition(mock_db, schedule_manager):
    """Test invalid schedule state transitions"""
    schedule_id = ObjectId()
    
    # Mock schedule in CANCELLED state
    mock_db.scheduled_operations.find_one.return_value = {
        "_id": schedule_id,
        "schedule_state": ScheduleState.CANCELLED.value,
        "metadata": {}
    }
    
    # Attempt to activate cancelled schedule
    result = await schedule_manager.activate_schedule(
        tool_operation_id="test_id",
        schedule_id=str(schedule_id)
    )
    
    assert result is False

if __name__ == "__main__":
    pytest.main(["-v", "test_tweet_scheduling.py"]) 