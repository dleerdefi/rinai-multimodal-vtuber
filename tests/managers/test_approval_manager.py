import pytest
import os
import sys
from pathlib import Path
from datetime import datetime, UTC
from dotenv import load_dotenv
from bson.objectid import ObjectId
from typing import List, Dict
import asyncio
import logging

# Add project root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

# Load environment variables before imports
load_dotenv(dotenv_path=Path(project_root) / '.env')

from src.managers.approval_manager import ApprovalManager, ApprovalState, ApprovalAction
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService
from src.db.mongo_manager import MongoManager
from src.db.db_schema import ToolOperationState, OperationStatus

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

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
        await db.tool_operations.delete_many({})
        await db.tool_items.delete_many({})
    finally:
        await MongoManager.close()

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()

@pytest.mark.asyncio
async def test_start_approval_flow():
    """Test starting approval flow"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )

        # Create test data
        session_id = "test_approval_session"
        
        # Start operation with correct parameters
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test_approval",
                "state": ToolOperationState.COLLECTING.value,
                "operation_metadata": {
                    "test": "data"
                },
                "requires_approval": True
            }
        )
        
        tool_operation_id = str(operation['_id'])

        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)
        
        # Start approval flow
        result = await approval_manager.start_approval_flow(
            session_id=session_id,
            tool_operation_id=tool_operation_id,
            items=items
        )
        
        # Verify result structure
        assert result["approval_state"] == ApprovalState.AWAITING_APPROVAL.value
        assert "response" in result
        assert "data" in result
        assert result["data"]["pending_count"] == len(items)
        
        # Verify items were updated to APPROVING
        updated_items = await db.tool_items.find({
            "tool_operation_id": tool_operation_id
        }).to_list(None)
        
        assert len(updated_items) == 2, "Not all items were found after update"
        for item in updated_items:
            assert item["state"] == ToolOperationState.APPROVING.value
            assert item["status"] == OperationStatus.PENDING.value
        
        # Verify operation state
        operation = await tool_state_manager.get_operation(session_id)
        assert operation["state"] == ToolOperationState.APPROVING.value
        assert operation["metadata"]["approval_state"] == ApprovalState.AWAITING_APPROVAL.value

    except Exception as e:
        logger.error(f"Test error in start approval flow: {e}")
        raise
    finally:
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": tool_operation_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_approval_workflow_with_regeneration():
    """Test complete approval workflow including regeneration"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup managers
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )

        # Create test data
        session_id = "test_regeneration_session"
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test command",
                "topic": "test topic",
                "requires_approval": True
            }
        )
        tool_operation_id = str(operation['_id'])

        # Create initial items
        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)
        
        # 1. Start approval flow
        await approval_manager.start_approval_flow(
            session_id=session_id,
            tool_operation_id=tool_operation_id,
            items=items
        )
        
        # 2. Partial approval (approve item 1, reject item 2)
        await approval_manager.process_approval_response(
            message="approve item 1, regenerate item 2",
            session_id=session_id,
            content_type="test",
            tool_operation_id=tool_operation_id,
            handlers={
                ApprovalAction.PARTIAL_APPROVAL.value: lambda **kwargs: approval_manager.handle_partial_approval(
                    session_id=kwargs['session_id'],
                    tool_operation_id=kwargs['tool_operation_id'],
                    approved_indices=[0],
                    items=items
                )
            }
        )
        
        # Verify states after partial approval
        all_items = await db.tool_items.find({
            "tool_operation_id": tool_operation_id
        }).to_list(None)
        
        # Should have 2 items: 1 approved, 1 rejected
        assert len(all_items) == 2, "Expected 2 items after partial approval"
        
        approved_items = [i for i in all_items if i["status"] == OperationStatus.APPROVED.value]
        rejected_items = [i for i in all_items if i["status"] == OperationStatus.REJECTED.value]
        
        assert len(approved_items) == 1, "Expected 1 approved item"
        assert approved_items[0]["state"] == ToolOperationState.EXECUTING.value
        
        assert len(rejected_items) == 1, "Expected 1 rejected item"
        assert rejected_items[0]["state"] == ToolOperationState.COMPLETED.value

        # Verify operation state
        operation = await tool_state_manager.get_operation(session_id)
        assert operation["state"] == ToolOperationState.COLLECTING.value
        assert operation["metadata"]["approval_state"] == ApprovalState.PARTIALLY_APPROVED.value

    except Exception as e:
        logger.error(f"Test error in regeneration workflow: {e}")
        raise
    finally:
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": tool_operation_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_regenerate_all_flow():
    """Test complete regeneration workflow"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup managers
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )

        # Create test data
        session_id = "test_regenerate_session"
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test command",
                "topic": "test topic",
                "requires_approval": True
            }
        )
        tool_operation_id = str(operation['_id'])

        # Create initial items
        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)
        
        # 1. Start approval flow
        await approval_manager.start_approval_flow(
            session_id=session_id,
            tool_operation_id=tool_operation_id,
            items=items
        )

        # 2. Request regeneration of all items
        regenerate_result = await approval_manager.process_approval_response(
            message="regenerate all items",
            session_id=session_id,
            content_type="test",
            tool_operation_id=tool_operation_id,
            handlers={
                ApprovalAction.REGENERATE_ALL.value: lambda **kwargs: approval_manager.handle_regenerate_all(
                    session_id=kwargs['session_id'],
                    tool_operation_id=kwargs['tool_operation_id']
                )
            }
        )
        
        # Verify regeneration result structure
        assert regenerate_result["status"] == "regeneration_needed"
        assert regenerate_result["regenerate_count"] == 2
        assert "response" in regenerate_result
        
        # Verify items moved to COMPLETED and REJECTED
        items_after_reject = await db.tool_items.find({
            "tool_operation_id": tool_operation_id
        }).to_list(None)
        
        # Update expectations: items should be COMPLETED, not COLLECTING
        assert all(item["state"] == ToolOperationState.COMPLETED.value for item in items_after_reject), \
            "All items should be in COMPLETED state"
        assert all(item["status"] == OperationStatus.REJECTED.value for item in items_after_reject), \
            "All items should be REJECTED"
        
        # Verify operation state
        operation_after_reject = await tool_state_manager.get_operation(session_id)
        assert operation_after_reject["metadata"]["approval_state"] == ApprovalState.REGENERATING.value
        assert "items_rejected" in operation_after_reject["metadata"]
        assert operation_after_reject["metadata"]["items_rejected"] == 2

        logger.info("Regenerate all flow test completed successfully")

    except Exception as e:
        logger.error(f"Test error in regenerate all flow: {e}")
        raise
    finally:
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": tool_operation_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_handle_exit_scenarios():
    """Test different exit scenarios"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )

        # Test Case 1: Manual Exit with Mixed States
        session_id = "test_exit_session_1"
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test_exit",
                "requires_approval": True
            }
        )
        tool_operation_id = str(operation['_id'])

        # Create initial items
        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "state": ToolOperationState.COLLECTING.value,
                "status": OperationStatus.PENDING.value,
                "tool_operation_id": tool_operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)

        # Start approval flow
        await approval_manager.start_approval_flow(
            session_id=session_id,
            tool_operation_id=tool_operation_id,
            items=items
        )

        # Partial approval (approve first item)
        await approval_manager.process_approval_response(
            message="approve item 1",
            session_id=session_id,
            content_type="test",
            tool_operation_id=tool_operation_id,
            handlers={
                ApprovalAction.PARTIAL_APPROVAL.value: lambda **kwargs: approval_manager.handle_partial_approval(
                    session_id=kwargs['session_id'],
                    tool_operation_id=kwargs['tool_operation_id'],
                    approved_indices=[0],
                    items=items
                )
            }
        )

        # Test manual exit with operation_id
        exit_result = await approval_manager.handle_exit(
            session_id=session_id,
            tool_operation_id=tool_operation_id,  # Add operation_id here
            success=False,
            tool_type="test_tool"
        )

        # Verify exit response format
        assert exit_result["state"] == "cancelled"
        assert "response" in exit_result
        assert exit_result["data"]["completion_type"] == "cancelled"

        # Verify states after exit
        final_items = await db.tool_items.find({
            "tool_operation_id": tool_operation_id
        }).to_list(None)

        # Verify approved item remains unchanged
        approved_items = [i for i in final_items if i["status"] == OperationStatus.APPROVED.value]
        assert len(approved_items) == 1
        assert approved_items[0]["state"] == ToolOperationState.EXECUTING.value

        # Verify pending item was cancelled
        cancelled_items = [i for i in final_items if i["status"] == OperationStatus.REJECTED.value]
        assert len(cancelled_items) == 1
        assert cancelled_items[0]["state"] == ToolOperationState.COMPLETED.value

        # Verify operation state
        final_operation = await tool_state_manager.get_operation(session_id)
        assert final_operation["state"] == ToolOperationState.COMPLETED.value
        assert final_operation["metadata"]["approval_state"] == ApprovalState.APPROVAL_CANCELLED.value

        logger.info("Exit scenarios test completed successfully")

    except Exception as e:
        logger.error(f"Test error in exit scenarios: {e}")
        raise

    finally:
        # Cleanup
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": tool_operation_id})
        await MongoManager.close()

if __name__ == "__main__":
    pytest.main(["-v", "test_approval_manager.py", "-s"])