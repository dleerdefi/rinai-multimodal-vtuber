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
                "status": ToolOperationState.COLLECTING.value,
                "operation_metadata": {
                    "test": "data"
                },
                "requires_approval": True
            }
        )
        
        operation_id = str(operation['_id'])

        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id  # Add session_id to items
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id  # Add session_id to items
            }
        ]
        
        # Insert test items
        await db.tool_items.insert_many(items)
        
        # Verify items were inserted
        initial_items = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)
        assert len(initial_items) == 2, "Items not properly inserted"
        
        # Start approval flow
        result = await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=items
        )
        
        # Verify result structure
        assert result["status"] == "awaiting_approval"
        assert "response" in result
        assert "data" in result
        assert result["data"]["pending_count"] == len(items)
        
        # Verify items were updated to APPROVING
        updated_items = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)
        
        # Debug logging
        logger.info(f"Updated items statuses: {[item['status'] for item in updated_items]}")
        
        assert len(updated_items) == 2, "Not all items were found after update"
        assert all(item["status"] == ToolOperationState.APPROVING.value for item in updated_items), \
            f"Items not in APPROVING state: {[item['status'] for item in updated_items]}"
        
        # Verify operation state
        operation = await tool_state_manager.get_operation(session_id)
        assert operation is not None, "Operation not found"
        assert operation["state"] == ToolOperationState.APPROVING.value
        assert operation["metadata"]["approval_state"] == ApprovalState.AWAITING_INITIAL.value
        
        logger.info("Start approval flow test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in start approval flow: {e}")
        raise
        
    finally:
        # Cleanup
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": operation_id})
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
        session_id = "test_approval_session"
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test command",
                "topic": "test topic",
                "requires_approval": True
            }
        )
        operation_id = str(operation['_id'])

        # Create initial items
        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)
        
        # 1. Start approval flow
        result = await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=items
        )
        
        assert result["status"] == "awaiting_approval"
        
        # 2. Partial approval (reject item 2)
        partial_approval_result = await approval_manager.process_approval_response(
            message="approve item 1, regenerate item 2",
            session_id=session_id,
            content_type="test",
            content_id=operation_id,
            handlers={
                ApprovalAction.PARTIAL_APPROVAL.value: lambda **kwargs: approval_manager.handle_partial_approval(
                    session_id=kwargs['session_id'],
                    operation_id=kwargs['content_id'],
                    approved_indices=[0],
                    items=items
                )
            }
        )
        
        # Verify item states after partial approval
        updated_items = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)
        
        approved_item = next(item for item in updated_items if item["content"] == "Test item 1")
        rejected_item = next(item for item in updated_items if item["content"] == "Test item 2")
        
        assert approved_item["status"] == ToolOperationState.EXECUTING.value
        assert rejected_item["status"] == ToolOperationState.COLLECTING.value
        
        # 3. Simulate regeneration of rejected item
        regenerated_item = {
            "_id": ObjectId(),
            "content": "Regenerated item 2",
            "status": ToolOperationState.COLLECTING.value,
            "tool_operation_id": operation_id,
            "session_id": session_id
        }
        
        await db.tool_items.insert_one(regenerated_item)
        
        # 4. Start approval flow for regenerated item
        regeneration_approval = await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=[regenerated_item]
        )
        
        assert regeneration_approval["status"] == "awaiting_approval"
        
        # 5. Approve regenerated item
        final_approval = await approval_manager.process_approval_response(
            message="approve all",
            session_id=session_id,
            content_type="test",
            content_id=operation_id,
            handlers={
                ApprovalAction.FULL_APPROVAL.value: lambda **kwargs: approval_manager.handle_full_approval(
                    session_id=kwargs['session_id'],
                    operation_id=kwargs['content_id']  # Map content_id to operation_id
                )
            }
        )   
        
        # Verify final states
        final_items = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)
        
        assert all(item["status"] == ToolOperationState.EXECUTING.value for item in final_items)
        
        # Verify operation state
        final_operation = await tool_state_manager.get_operation(session_id)
        assert final_operation["state"] == ToolOperationState.EXECUTING.value
        
        logger.info("Approval workflow with regeneration test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in approval workflow: {e}")
        raise
        
    finally:
        # Cleanup
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": operation_id})
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
        # Setup
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )

        # 1. Create initial operation and items
        session_id = "test_regenerate_session"
        operation = await tool_state_manager.start_operation(
            session_id=session_id,
            operation_type="test_tool",
            initial_data={
                "command": "test_regenerate",
                "requires_approval": True
            }
        )
        operation_id = str(operation['_id'])

        # Create initial items
        initial_items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(initial_items)
        
        # 2. Start initial approval flow
        start_result = await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=initial_items
        )
        
        assert start_result["status"] == "awaiting_approval"
        
        # 3. Request regeneration of all items
        regenerate_result = await approval_manager.process_approval_response(
            message="regenerate all items",
            session_id=session_id,
            content_type="test",
            content_id=operation_id,
            handlers={
                ApprovalAction.REGENERATE_ALL.value: lambda **kwargs: approval_manager.handle_regenerate_all(
                    session_id=kwargs['session_id'],
                    operation_id=kwargs['content_id']
                )
            }
        )
        
        # Verify items moved to COLLECTING and REJECTED
        items_after_reject = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)
        
        assert all(item["status"] == ToolOperationState.COLLECTING.value for item in items_after_reject)
        assert all(item["operation_status"] == OperationStatus.REJECTED.value for item in items_after_reject)
        
        # 4. Simulate regeneration with new items
        regenerated_items = [
            {
                "_id": ObjectId(),
                "content": "Regenerated item 1",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Regenerated item 2",
                "status": ToolOperationState.COLLECTING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(regenerated_items)
        
        # 5. Start approval flow for regenerated items
        regenerated_approval = await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=regenerated_items
        )
        
        assert regenerated_approval["status"] == "awaiting_approval"
        
        # 6. Approve regenerated items
        final_approval = await approval_manager.process_approval_response(
            message="approve all",
            session_id=session_id,
            content_type="test",
            content_id=operation_id,
            handlers={
                ApprovalAction.FULL_APPROVAL.value: lambda **kwargs: approval_manager.handle_full_approval(
                    session_id=kwargs['session_id'],
                    operation_id=kwargs['content_id']
                )
            }
        )
        
        # Verify final states
        final_items = await db.tool_items.find({
            "tool_operation_id": operation_id,
            "status": ToolOperationState.EXECUTING.value
        }).to_list(None)
        
        assert len(final_items) == 2, "Not all items in EXECUTING state"
        assert all(item["operation_status"] == OperationStatus.APPROVED.value for item in final_items)
        
        # Verify operation state
        final_operation = await tool_state_manager.get_operation(session_id)
        assert final_operation["state"] == ToolOperationState.EXECUTING.value
        assert final_operation["metadata"]["approval_state"] == ApprovalState.APPROVAL_FINISHED.value
        
        logger.info("Regenerate all flow test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in regenerate all flow: {e}")
        raise
        
    finally:
        # Cleanup
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": operation_id})
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
        operation_id = str(operation['_id'])

        # Create initial items
        items = [
            {
                "_id": ObjectId(),
                "content": "Test item 1",
                "status": ToolOperationState.COLLECTING.value,
                "operation_status": OperationStatus.PENDING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            },
            {
                "_id": ObjectId(),
                "content": "Test item 2",
                "status": ToolOperationState.COLLECTING.value,
                "operation_status": OperationStatus.PENDING.value,
                "tool_operation_id": operation_id,
                "session_id": session_id
            }
        ]
        
        await db.tool_items.insert_many(items)

        # Start approval flow
        await approval_manager.start_approval_flow(
            session_id=session_id,
            operation_id=operation_id,
            items=items
        )

        # Partial approval (approve first item)
        await approval_manager.process_approval_response(
            message="approve item 1",
            session_id=session_id,
            content_type="test",
            content_id=operation_id,
            handlers={
                ApprovalAction.PARTIAL_APPROVAL.value: lambda **kwargs: approval_manager.handle_partial_approval(
                    session_id=kwargs['session_id'],
                    operation_id=kwargs['content_id'],
                    approved_indices=[0],
                    items=items
                )
            }
        )

        # Test manual exit with operation_id
        exit_result = await approval_manager.handle_exit(
            session_id=session_id,
            operation_id=operation_id,
            success=False,
            tool_type="test_tool"
        )

        # Verify exit response format
        assert exit_result["status"] == "cancelled"
        assert "response" in exit_result
        assert exit_result["data"]["completion_type"] == "cancelled"

        # Verify states after exit
        final_items = await db.tool_items.find({
            "tool_operation_id": operation_id
        }).to_list(None)

        # Verify approved item remains unchanged
        approved_items = [i for i in final_items if i["operation_status"] == OperationStatus.APPROVED.value]
        assert len(approved_items) == 1
        assert approved_items[0]["status"] == ToolOperationState.EXECUTING.value

        # Verify pending item was cancelled
        cancelled_items = [i for i in final_items if i["operation_status"] == OperationStatus.REJECTED.value]
        assert len(cancelled_items) == 1
        assert cancelled_items[0]["status"] == ToolOperationState.CANCELLED.value

        # Verify operation state
        final_operation = await tool_state_manager.get_operation(session_id)
        assert final_operation["state"] == ToolOperationState.CANCELLED.value
        assert final_operation["metadata"]["approval_state"] == ApprovalState.APPROVAL_CANCELLED.value

        logger.info("Exit scenarios test completed successfully")

    except Exception as e:
        logger.error(f"Test error in exit scenarios: {e}")
        raise

    finally:
        # Cleanup
        await db.tool_operations.delete_many({"session_id": session_id})
        await db.tool_items.delete_many({"tool_operation_id": operation_id})
        await MongoManager.close()

if __name__ == "__main__":
    pytest.main(["-v", "test_approval_manager.py", "-s"])