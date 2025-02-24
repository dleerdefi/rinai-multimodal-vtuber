import pytest
from unittest.mock import AsyncMock, Mock, MagicMock
import logging
from datetime import datetime, UTC
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from src.managers.tool_state_manager import ToolStateManager
from src.db.enums import (
    ToolOperationState, 
    ToolType, 
    ContentType, 
    OperationStatus, 
    ScheduleState
)
from src.db.db_schema import RinDB, ToolOperation

# Set up logging
logger = logging.getLogger(__name__)

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

class MockDB(RinDB):
    """Mock RinDB that matches the actual implementation"""
    def __init__(self):
        self.tool_operations = MockCollection()
        self.tool_items = MockCollection()
        self.scheduled_operations = MockCollection()
        
    async def set_tool_operation_state(self, session_id: str, operation_data: dict) -> dict:
        """Mock implementation of set_tool_operation_state"""
        result = await self.tool_operations.find_one_and_update(
            {"session_id": session_id},
            {"$set": operation_data},
            upsert=True,
            return_document=True
        )
        return result
        
    async def get_tool_operation_state(self, session_id: str) -> dict:
        """Mock implementation of get_tool_operation_state"""
        return await self.tool_operations.find_one({"session_id": session_id})

@pytest.fixture(autouse=True)
async def setup_teardown():
    """Setup and teardown for all tests"""
    try:
        yield
    finally:
        try:
            db = RinDB(AsyncIOMotorClient())
            if db:
                await db.tool_operations.delete_many({})
                await db.tool_items.delete_many({})
                await db.scheduled_operations.delete_many({})
        except Exception as e:
            logger.error(f"Error cleaning up database: {e}")

@pytest.fixture
def mock_db():
    """Create mock database - not async since we're just creating the mock"""
    return MockDB()

@pytest.fixture
def tool_state_manager(mock_db):
    """Create ToolStateManager instance - not async since we're just creating the instance"""
    return ToolStateManager(db=mock_db)

@pytest.mark.asyncio
async def test_start_operation(tool_state_manager):
    """Test starting a new tool operation"""
    operation_id = ObjectId()
    
    # Set up mock responses
    operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "tool_type": ToolType.TWITTER.value,
        "state": ToolOperationState.COLLECTING.value,
        "step": "analyzing",
        "input_data": {
            "command": "schedule tweets",
            "command_info": {"topic": "AI"}
        },
        "output_data": {
            "status": OperationStatus.PENDING.value,
            "content": [],
            "requires_approval": True,
            "pending_items": [],
            "approved_items": [],
            "rejected_items": [],
            "schedule_id": None
        },
        "metadata": {
            "state_history": [{
                "state": ToolOperationState.COLLECTING.value,
                "step": "analyzing",
                "timestamp": datetime.now(UTC).isoformat()
            }]
        },
        "created_at": datetime.now(UTC),
        "last_updated": datetime.now(UTC)
    }
    
    tool_state_manager.db.tool_operations.find_one_and_update.return_value = operation
    
    result = await tool_state_manager.start_operation(
        session_id="test_session",
        operation_type=ToolType.TWITTER.value,
        initial_data={
            "command": "schedule tweets",
            "command_info": {"topic": "AI"},
            "tool_registry": {
                "requires_approval": True,
                "requires_scheduling": False,
                "content_type": ContentType.TWEET.value
            }
        }
    )
    
    assert result is not None
    assert result["session_id"] == "test_session"
    assert result["tool_type"] == ToolType.TWITTER.value
    assert result["state"] == ToolOperationState.COLLECTING.value

@pytest.mark.asyncio
async def test_get_operation(tool_state_manager):
    """Test retrieving an existing operation"""
    operation_id = ObjectId()
    
    mock_operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.COLLECTING.value,
        "tool_type": "twitter",
        "step": "analyzing",
        "input_data": {"command": "test command"},
        "output_data": {"status": OperationStatus.PENDING.value},
        "metadata": {"state_history": []},
        "created_at": datetime.now(UTC),
        "last_updated": datetime.now(UTC)
    }
    
    tool_state_manager.db.tool_operations.find_one.return_value = mock_operation
    operation = await tool_state_manager.get_operation("test_session")
    
    assert operation is not None
    assert operation["_id"] == operation_id
    assert operation["state"] == ToolOperationState.COLLECTING.value

@pytest.mark.asyncio
async def test_update_operation_state(tool_state_manager):
    """Test updating operation state"""
    operation_id = ObjectId()
    
    # Mock initial operation
    initial_operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.COLLECTING.value,
        "tool_type": "twitter",
        "metadata": {},
        "output_data": {}
    }
    
    tool_state_manager.db.tool_operations.find_one.return_value = initial_operation
    tool_state_manager.db.tool_operations.update_one.return_value = MagicMock(modified_count=1)
    
    updated = await tool_state_manager.update_operation(
        session_id="test_session",
        tool_operation_id=str(operation_id),
        state=ToolOperationState.APPROVING.value,
        metadata={"generated_content": ["Tweet 1", "Tweet 2"]}
    )
    
    assert updated is True

@pytest.mark.asyncio
async def test_operation_lifecycle(tool_state_manager):
    """Test complete operation lifecycle"""
    operation_id = ObjectId()
    
    # Mock initial operation
    operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.COLLECTING.value,
        "tool_type": "twitter",
        "metadata": {
            "requires_approval": True,
            "requires_scheduling": True
        },
        "output_data": {}
    }
    
    # Set up mock responses for each stage
    tool_state_manager.db.tool_operations.find_one_and_update.return_value = operation
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    tool_state_manager.db.tool_operations.update_one.return_value = MagicMock(modified_count=1)
    
    # 1. Start operation
    result = await tool_state_manager.start_operation(
        session_id="test_session",
        operation_type="twitter",
        initial_data={"command": "schedule tweets"}
    )
    assert result["state"] == ToolOperationState.COLLECTING.value
    
    # Update mock for subsequent operations
    operation["state"] = ToolOperationState.APPROVING.value
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    
    # 2. Update to APPROVING
    updated = await tool_state_manager.update_operation(
        session_id="test_session",
        tool_operation_id=str(operation_id),
        state=ToolOperationState.APPROVING.value,
        metadata={"generated_content": ["Tweet 1", "Tweet 2"]}
    )
    assert updated is True
    
    # 3. Complete operation
    operation["state"] = ToolOperationState.COMPLETED.value
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    
    final_op = await tool_state_manager.get_operation_by_id(str(operation_id))
    assert final_op["state"] == ToolOperationState.COMPLETED.value

@pytest.mark.asyncio
async def test_create_tool_items(tool_state_manager):
    """Test creating tool items with proper state tracking"""
    operation_id = ObjectId()
    
    # Mock initial operation
    operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.COLLECTING.value,
        "tool_type": ToolType.TWITTER.value,
        "output_data": {"pending_items": []}
    }
    
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    
    items_data = [
        {
            "content": "Tweet 1",
            "metadata": {"source": "ai_generated"}
        },
        {
            "content": "Tweet 2",
            "metadata": {"source": "ai_generated"}
        }
    ]
    
    # Mock item creation responses
    tool_state_manager.db.tool_items.insert_one.side_effect = [
        MagicMock(inserted_id=ObjectId()) for _ in range(len(items_data))
    ]
    
    items = await tool_state_manager.create_tool_items(
        session_id="test_session",
        tool_operation_id=str(operation_id),
        items_data=items_data,
        content_type=ContentType.TWEET.value
    )
    
    assert len(items) == 2
    for item in items:
        assert item["state"] == ToolOperationState.COLLECTING.value
        assert item["content_type"] == ContentType.TWEET.value
        assert "state_history" in item["metadata"]

@pytest.mark.asyncio
async def test_regeneration_workflow(tool_state_manager):
    """Test regeneration of tool items"""
    operation_id = ObjectId()
    
    # Mock initial operation in APPROVING state
    operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.APPROVING.value,
        "tool_type": ToolType.TWITTER.value,
        "output_data": {"pending_items": []}
    }
    
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    
    # Create new items for regeneration
    new_items = [
        {
            "content": "Regenerated Tweet 1",
            "metadata": {"regeneration_count": 1}
        },
        {
            "content": "Regenerated Tweet 2",
            "metadata": {"regeneration_count": 1}
        }
    ]
    
    tool_state_manager.db.tool_items.insert_one.side_effect = [
        MagicMock(inserted_id=ObjectId()) for _ in range(len(new_items))
    ]
    
    regenerated_items = await tool_state_manager.create_regeneration_items(
        session_id="test_session",
        tool_operation_id=str(operation_id),
        items_data=new_items,
        content_type=ContentType.TWEET.value
    )
    
    assert len(regenerated_items) == 2
    for item in regenerated_items:
        assert item["state"] == ToolOperationState.COLLECTING.value
        assert "regeneration_count" in item["metadata"]

@pytest.mark.asyncio
async def test_invalid_state_transition(tool_state_manager):
    """Test invalid state transitions are handled properly"""
    operation_id = ObjectId()
    
    # Mock initial operation in COMPLETED state
    operation = {
        "_id": operation_id,
        "session_id": "test_session",
        "state": ToolOperationState.COMPLETED.value,
        "tool_type": ToolType.TWITTER.value,
        "metadata": {},
        "output_data": {}
    }
    
    tool_state_manager.db.tool_operations.find_one.return_value = operation
    
    # The update_operation should raise ValueError for invalid transition
    with pytest.raises(ValueError) as exc_info:
        await tool_state_manager.update_operation(
            session_id="test_session",
            tool_operation_id=str(operation_id),
            state=ToolOperationState.COLLECTING.value
        )
    
    assert "Invalid state transition" in str(exc_info.value)

@pytest.mark.asyncio
async def test_concurrent_operations(tool_state_manager):
    """Test handling multiple operations for the same session"""
    # Create first operation with specific ID
    op1_id = ObjectId()
    
    # Mock the database responses with different IDs
    tool_state_manager.db.tool_operations.find_one_and_update.side_effect = [
        {
            "_id": op1_id,
            "session_id": "test_session",
            "state": ToolOperationState.COLLECTING.value,
            "tool_type": ToolType.TWITTER.value,
            "metadata": {},
            "output_data": {},
            "input_data": {"command": "first operation"}
        },
        {
            "_id": ObjectId(),  # Different ID for second operation
            "session_id": "test_session",
            "state": ToolOperationState.COLLECTING.value,
            "tool_type": ToolType.TWITTER.value,
            "metadata": {},
            "output_data": {},
            "input_data": {"command": "second operation"}
        }
    ]
    
    # Execute operations
    op1 = await tool_state_manager.start_operation(
        session_id="test_session",
        operation_type=ToolType.TWITTER.value,
        initial_data={"command": "first operation"}
    )
    
    op2 = await tool_state_manager.start_operation(
        session_id="test_session",
        operation_type=ToolType.TWITTER.value,
        initial_data={"command": "second operation"}
    )
    
    assert str(op1["_id"]) != str(op2["_id"])
    assert op1["state"] == ToolOperationState.COLLECTING.value
    assert op2["state"] == ToolOperationState.COLLECTING.value

@pytest.mark.asyncio
async def test_error_handling(tool_state_manager):
    """Test error handling in operations"""
    operation_id = ObjectId()
    
    # Mock database error
    tool_state_manager.db.tool_operations.find_one.side_effect = Exception("Database error")
    
    # The error should be wrapped with our custom error message
    with pytest.raises(Exception) as exc_info:
        await tool_state_manager.get_operation("test_session")
    
    # Check both the custom message and original error
    error_msg = str(exc_info.value)
    assert "Database error" in error_msg 