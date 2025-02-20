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

from src.tools.post_tweets import TwitterTool
from src.tools.base import AgentDependencies
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService
from src.managers.approval_manager import ApprovalManager, ApprovalState, ApprovalAction
from src.db.mongo_manager import MongoManager
from src.db.db_schema import ToolOperationState, OperationStatus

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True  # This ensures our config takes precedence
)

@pytest.fixture(autouse=True)
async def setup_teardown():
    """Setup and teardown for all tests"""
    # Setup
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
        await db.tweets.delete_many({})
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
async def test_tweet_tool_workflow():
    """Test complete tweet tool workflow"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # 1. Setup
        deps = AgentDependencies(
            session_id="test_workflow_session",
            user_id="test_user_123",
            context={},
            tools_available=["twitter"]
        )
        
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )
        
        tweet_tool = TwitterTool(
            deps=deps,
            tool_state_manager=tool_state_manager,
            llm_service=llm_service,
            approval_manager=approval_manager
        )

        # 2. Initial Command - Analysis and Generation
        initial_command = "Generate 2 tweets about AI"
        result = await tweet_tool.run(initial_command)
        
        # Verify items were generated
        items = result.get('data', {}).get('items', [])
        assert len(items) > 0, "No items returned in approval flow"
        
        # 3. Approval Response
        approval_response = "Yes, these look good"
        approval_result = await tweet_tool.run(approval_response)
        
        logger.info("Tweet tool workflow test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in workflow: {e}")
        raise
        
    finally:
        # Cleanup
        await db.scheduled_operations.delete_many({"session_id": deps.session_id})
        await db.tool_operations.delete_many({"session_id": deps.session_id})
        await db.tool_items.delete_many({"session_id": deps.session_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_tweet_tool_cancellation():
    """Test cancellation of tweet tool workflow"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup
        deps = AgentDependencies(
            session_id="test_cancel_session",
            user_id="test_user_123",
            context={},
            tools_available=["twitter"]
        )
        
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )
        
        tweet_tool = TwitterTool(
            deps=deps,
            tool_state_manager=tool_state_manager,
            llm_service=llm_service,
            approval_manager=approval_manager
        )

        # Initial command and verify items created
        initial_command = "Generate 2 tweets about AI"
        result = await tweet_tool.run(initial_command)
        assert result.get('data', {}).get('items'), "No items returned for approval"
        
        # Send cancellation
        cancel_response = "cancel"
        cancel_result = await tweet_tool.run(cancel_response)
        
        logger.info("Tweet tool cancellation test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in cancellation: {e}")
        raise
        
    finally:
        # Cleanup
        await db.scheduled_operations.delete_many({"session_id": deps.session_id})
        await db.tool_operations.delete_many({"session_id": deps.session_id})
        await db.tool_items.delete_many({"session_id": deps.session_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_analyze_twitter_command():
    """Test just the command analysis function"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # Setup
        logger.info("Setting up test dependencies...")
        deps = AgentDependencies(
            session_id="test_analysis_session",
            user_id="test_user_123",
            context={},
            tools_available=["twitter"]
        )
        
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )
        
        logger.info("Initializing tweet tool...")
        tweet_tool = TwitterTool(
            deps=deps,
            tool_state_manager=tool_state_manager,
            llm_service=llm_service,
            approval_manager=approval_manager
        )

        # Test command analysis
        command = "Generate 2 tweets about AI"
        logger.info(f"Testing command analysis with: {command}")
        result = await tweet_tool._analyze_twitter_command(command)
        
        # Verify result structure
        assert result is not None, "Analysis result is None"
        assert "schedule_id" in result, "No schedule_id in result"
        assert "topic" in result, "No topic in result"
        assert "item_count" in result, "No item_count in result"
        
        logger.info("Command analysis test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in command analysis: {e}")
        raise
        
    finally:
        # Cleanup
        await db.scheduled_operations.delete_many({"session_id": deps.session_id})
        await db.tool_operations.delete_many({"session_id": deps.session_id})
        await MongoManager.close()

@pytest.mark.asyncio
async def test_tweet_tool_partial_approval():
    """Test partial approval and regeneration workflow"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    db = MongoManager.get_db()
    
    try:
        # 1. Setup
        deps = AgentDependencies(
            session_id="test_partial_session",
            user_id="test_user_123",
            context={},
            tools_available=["twitter"]
        )
        
        tool_state_manager = ToolStateManager(db=db)
        llm_service = LLMService()
        approval_manager = ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=db,
            llm_service=llm_service
        )
        
        tweet_tool = TwitterTool(
            deps=deps,
            tool_state_manager=tool_state_manager,
            llm_service=llm_service,
            approval_manager=approval_manager
        )

        # 2. Initial Command - Generate tweets
        initial_command = "Generate 2 tweets about AI"
        result = await tweet_tool.run(initial_command)
        
        # Verify items were generated and capture operation_id
        items = result.get('data', {}).get('items', [])
        assert len(items) > 0, "No items returned in approval flow"
        initial_item_ids = [item['_id'] for item in items]
        
        # Get operation_id from the initial operation
        operation = await tool_state_manager.get_operation(deps.session_id)
        operation_id = str(operation['_id'])  # Capture the operation_id
        
        # 3. Partial Approval Response
        partial_approval = "approve the first one, regenerate the second"
        approval_result = await tweet_tool.run(partial_approval)
        
        # 4. Verify state transitions and item statuses
        operation = await tool_state_manager.get_operation(deps.session_id)
        assert operation['state'] == ToolOperationState.COLLECTING.value, "Operation not in collecting state"
        
        # Check approved item with operation_id
        approved_item = await db.tool_items.find_one({
            "_id": ObjectId(initial_item_ids[0]),
            "tool_operation_id": operation_id  # Include operation_id in query
        })
        assert approved_item['status'] == ToolOperationState.EXECUTING.value, "First item not marked as executing"
        assert approved_item['operation_status'] == OperationStatus.APPROVED.value, "First item not marked as approved"
        
        # Check regenerated items with operation_id
        regenerated_items = await db.tool_items.find({
            "session_id": deps.session_id,
            "tool_operation_id": operation_id,  # Include operation_id in query
            "_id": {"$nin": [ObjectId(id) for id in initial_item_ids]}
        }).to_list(None)
        
        assert len(regenerated_items) > 0, "No items were regenerated"
        
        # 5. Verify metadata
        operation_metadata = operation.get('metadata', {})
        assert operation_metadata.get('approval_state') == ApprovalState.REGENERATING.value, "Operation not in regenerating state"
        assert operation_metadata.get('regeneration_pending') is True, "Regeneration not marked as pending"
        
        logger.info("Tweet tool partial approval test completed successfully")
        
    except Exception as e:
        logger.error(f"Test error in partial approval: {e}")
        raise
        
    finally:
        # Cleanup
        await db.scheduled_operations.delete_many({"session_id": deps.session_id})
        await db.tool_operations.delete_many({"session_id": deps.session_id})
        await db.tool_items.delete_many({"session_id": deps.session_id})
        await MongoManager.close()

if __name__ == "__main__":
    pytest.main(["-v", "test_tweet_tool.py", "-s"]) 