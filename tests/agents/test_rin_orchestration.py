import pytest
import os
from pathlib import Path
from datetime import datetime, UTC, timedelta
from dotenv import load_dotenv
from bson.objectid import ObjectId
import logging
from unittest.mock import Mock, AsyncMock, MagicMock, patch, call
from typing import Dict, Optional
import uuid
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

# Load environment variables before imports
load_dotenv()

from src.agents.rin.agent import RinAgent
from src.tools.orchestrator import Orchestrator
from src.managers.tool_state_manager import ToolStateManager
from src.managers.agent_state_manager import AgentStateManager
from src.managers.approval_manager import ApprovalManager
from src.managers.schedule_manager import ScheduleManager
from src.services.llm_service import LLMService
from src.services.schedule_service import ScheduleService
from src.db.mongo_manager import MongoManager
from src.db.db_schema import RinDB
from src.db.enums import (
    AgentState, 
    ToolOperationState, 
    OperationStatus, 
    ContentType, 
    ToolType,
    ApprovalState,
    ScheduleState
)
from src.tools.post_tweets import TwitterTool
from src.tools.base import AgentResult, AgentDependencies
from src.agents.rin.context_manager import RinContext
from src.utils.trigger_detector import TriggerDetector

logger = logging.getLogger(__name__)

class MockCollection:
    """Mock MongoDB collection with async methods"""
    def __init__(self):
        self.find_one = AsyncMock()
        self.find_one_and_update = AsyncMock()
        self.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        self.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
        self.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
        self.delete_many = AsyncMock()
        self.find = AsyncMock()
        self.create_index = AsyncMock()
        
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        self.find.return_value = mock_cursor

class MockDB(RinDB):
    """Mock RinDB that inherits from actual RinDB"""
    def __init__(self):
        # Skip parent class initialization
        self._initialized = False
        self._client = None
        self.db = None
        
        # Collections will be set up when client is assigned
        self.messages = None
        self.context_configs = None
        self.tool_items = None
        self.tool_operations = None
        self.tool_executions = None
        self.scheduled_operations = None
        self.tweets = None
        self.tweet_schedules = None

    @property
    def client(self):
        return self._client

    @client.setter
    def client(self, client):
        """Set up collections when client is assigned"""
        self._client = client
        self.db = client['rin_multimodal']
        
        # Set up collections
        self.messages = self.db['rin.messages']
        self.context_configs = self.db['rin.context_configs']
        self.tool_items = self.db['rin.tool_items']
        self.tool_operations = self.db['rin.tool_operations']
        self.tool_executions = self.db['rin.tool_executions']
        self.scheduled_operations = self.db['rin.scheduled_operations']
        self.tweets = self.db['rin.tweets']
        self.tweet_schedules = self.db['rin.tweet_schedules']

    async def initialize(self):
        """Mock initialization"""
        if not self._initialized:
            self._initialized = True
            # Verify collections are set up
            if not all([self.messages, self.context_configs, self.tool_items,
                       self.tool_operations, self.tool_executions, 
                       self.scheduled_operations, self.tweets, self.tweet_schedules]):
                raise RuntimeError("Collections not properly initialized")
        return True
        
    async def is_initialized(self):
        """Mock initialization check"""
        return self._initialized
        
    async def get_tool_operation_state(self, session_id: str):
        return {
            "session_id": session_id,
            "state": ToolOperationState.EXECUTING.value,
            "tool_type": ToolType.TWITTER.value,
            "metadata": {
                "requires_approval": False,
                "requires_scheduling": False
            }
        }
        
    async def set_tool_operation_state(self, session_id: str, operation_data: Dict):
        return operation_data

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def global_mongo_client():
    """Create a single MongoDB client for all tests"""
    # Use a consistent database name for all tests in the session
    test_db_name = f"rin_test_{str(uuid.uuid4()).replace('-', '')}"
    client = AsyncIOMotorClient(f"mongodb://localhost:27017")
    
    # Create clean database
    logger.info(f"Setting up test database: {test_db_name}")
    try:
        await client.drop_database(test_db_name)
    except Exception as e:
        logger.warning(f"Error cleaning database before tests: {e}")
    
    yield client, test_db_name
    
    # Clean up after all tests
    logger.info(f"Cleaning up test database: {test_db_name}")
    try:
        await client.drop_database(test_db_name)
    except Exception as e:
        logger.warning(f"Error cleaning database after tests: {e}")

@pytest.fixture(autouse=True)
async def setup_db(global_mongo_client):
    """Initialize MongoDB for each test"""
    client, test_db_name = global_mongo_client
    mongo_uri = f"mongodb://localhost:27017/{test_db_name}"
    
    # Close any existing connection first
    try:
        await MongoManager.close()
    except:
        pass
    
    # Reset class variables in MongoManager to ensure clean state
    MongoManager._instance = None
    MongoManager._db = None
    MongoManager._initialized = False
    
    # Initialize with test database
    logger.info(f"Initializing MongoDB: {mongo_uri}")
    await MongoManager.initialize(mongo_uri)
    
    # Verify initialization
    assert MongoManager._instance is not None, "MongoManager._instance is None after initialization"
    assert MongoManager._db is not None, "MongoManager._db is None after initialization"
    
    # Get database and verify it's working
    db = MongoManager.get_db()
    logger.info(f"MongoDB initialized, collections: {await db.db.list_collection_names()}")
    
    # Return the database for use in tests
    yield db

@pytest.fixture
async def rin_agent_with_deps(setup_db):
    """Create RinAgent with minimal mocking needed for tests"""
    # Get the database URI from MongoManager
    mongo_uri = f"mongodb://{MongoManager._instance.address[0]}:{MongoManager._instance.address[1]}/{MongoManager._instance.database.name}"
    logger.info(f"Creating RinAgent with URI: {mongo_uri}")
    
    # Create agent
    agent = RinAgent(mongo_uri=mongo_uri)
    await agent.initialize()
    
    # Create TwitterTool with dependencies
    deps = AgentDependencies(session_id=str(ObjectId()))
    twitter_tool = TwitterTool(deps=deps)
    
    # Setup tool
    twitter_tool.tool_state_manager = agent.tool_state_manager
    twitter_tool.llm_service = agent.orchestrator.llm_service
    twitter_tool.approval_manager = agent.orchestrator.approval_manager
    twitter_tool.schedule_manager = agent.orchestrator.schedule_manager
    twitter_tool.db = setup_db
    
    # Add TwitterTool to orchestrator
    agent.orchestrator.tools[ToolType.TWITTER.value] = twitter_tool
    
    return agent, twitter_tool

@pytest.fixture
async def rin_agent_with_mocks(setup_db):
    """Create RinAgent with minimal mocking - only mock the approval response"""
    # Get the database URI from MongoManager
    mongo_uri = f"mongodb://{MongoManager._instance.address[0]}:{MongoManager._instance.address[1]}/{MongoManager._instance.database.name}"
    logger.info(f"Creating RinAgent with URI: {mongo_uri}")
    
    # Create agent
    agent = RinAgent(mongo_uri=mongo_uri)
    await agent.initialize()
    
    # Make sure we have a TwitterTool instance properly registered
    # This ensures the real TwitterTool.analyze_command is used
    # which is critical for determining requires_approval and requires_scheduling
    twitter_tool = agent.orchestrator.tools.get(ToolType.TWITTER.value)
    if not twitter_tool:
        # If the tool wasn't registered, register it now
        deps = AgentDependencies(session_id="test_session")
        twitter_tool = TwitterTool(deps=deps)
        twitter_tool.tool_state_manager = agent.tool_state_manager
        twitter_tool.llm_service = agent.orchestrator.llm_service
        twitter_tool.approval_manager = agent.orchestrator.approval_manager
        twitter_tool.schedule_manager = agent.orchestrator.schedule_manager
        twitter_tool.db = setup_db
        agent.orchestrator.tools[ToolType.TWITTER.value] = twitter_tool
    
    # Verify the tool has the correct analyze_command method
    assert hasattr(twitter_tool, 'analyze_command'), "TwitterTool missing analyze_command method"
    
    # Mock only the trigger detector to guarantee tool detection
    agent.trigger_detector.get_specific_tool_type = Mock(return_value=ToolType.TWITTER.value)
    agent.trigger_detector.detect_tool_trigger = Mock(return_value=True)
    
    # Create mock approval analyzer that simulates user approval
    mock_approval_analyzer = Mock()
    
    # Define more complete mock implementation
    async def mock_analyze_response(user_response, current_items):
        """Mock implementation of analyze_response"""
        logger.info(f"Mock analyzing: '{user_response}' for {len(current_items)} items")
        
        if any(term in user_response.lower() for term in ["approve all", "all good", "looks good"]):
            return {
                "action": "full_approval",
                "indices": list(range(min(3, len(current_items)))),  # Up to 3 items
                "feedback": "All tweets approved",
                "reasoning": "User approved all tweets"
            }
        elif any(term in user_response.lower() for term in ["reject", "regenerate"]):
            return {
                "action": "regenerate_all",
                "indices": [],
                "regenerate_indices": list(range(min(3, len(current_items)))),
                "feedback": "Regenerate all tweets",
                "reasoning": "User requested regeneration"
            }
        elif any(term in user_response.lower() for term in ["first", "1"]):
            return {
                "action": "partial_approval",
                "indices": [0],  # Just the first item
                "regenerate_indices": list(range(1, min(3, len(current_items)))),
                "feedback": "Approve first tweet, regenerate others",
                "reasoning": "User only liked the first tweet"
            }
        else:
            return {
                "action": "awaiting_input",
                "feedback": "Please specify which tweets you'd like to approve",
                "reasoning": "User input unclear"
            }
    
    mock_approval_analyzer.analyze_response = AsyncMock(side_effect=mock_analyze_response)
    
    mock_approval_analyzer.format_items_for_review = Mock(
        return_value="1. Tweet about crypto\n2. Another tweet\n3. Final tweet"
    )
    
    mock_approval_analyzer.create_error_response = Mock(
        side_effect=lambda msg, **kwargs: {
            "response": f"Error: {msg}", 
            "status": "error",
            "requires_tts": True
        }
    )
    
    mock_approval_analyzer.create_awaiting_response = Mock(
        return_value={
            "response": "Please let me know which tweets you'd like to approve.",
            "status": "awaiting_input",
            "requires_tts": True
        }
    )
    
    mock_approval_analyzer.create_exit_response = Mock(
        side_effect=lambda success, tool_type: {
            "response": "Operation completed successfully" if success else "Operation cancelled",
            "status": "completed" if success else "cancelled",
            "requires_tts": True
        }
    )
    
    # Replace only the analyzer in the approval manager
    agent.orchestrator.approval_manager.analyzer = mock_approval_analyzer
    
    return agent

@pytest.mark.asyncio
async def test_complete_tweet_lifecycle(rin_agent_with_deps):
    """Test complete lifecycle of tweet operation"""
    agent, twitter_tool = await rin_agent_with_deps
    session_id = twitter_tool.deps.session_id
    
    try:
        # Test implementation...
        initial_command = "Generate 2 tweets about AI"
        result = await agent.orchestrator.handle_tool_operation(
            message=initial_command,
            session_id=session_id,
            tool_type=ToolType.TWITTER.value
        )
        
        # Verify result
        assert result is not None
        
        # Get operation from DB
        operation = await agent.tool_state_manager.get_operation(session_id)
        assert operation is not None
        assert operation["state"] == ToolOperationState.COLLECTING.value
        
    except Exception as e:
        logger.error(f"Test error: {e}")
        raise

async def dump_db_state(db, session_id: str):
    """Dump database state for debugging purposes"""
    logger.info("=== DATABASE STATE ===")
    
    # Get operation state
    operation = await db.get_tool_operation_state(session_id)
    if operation:
        logger.info(f"Tool Operation: {operation.get('state')} (ID: {operation.get('_id')})")
        logger.info(f"  - Metadata: {operation.get('metadata', {})}")
        logger.info(f"  - Input data: {operation.get('input_data', {})}")
        logger.info(f"  - Output data: {operation.get('output_data', {})}")
    else:
        logger.info("No tool operation found")
    
    # Get tool items
    tool_items = await db.tool_items.find({"session_id": session_id}).to_list(None)
    logger.info(f"Tool Items ({len(tool_items)}):")
    for idx, item in enumerate(tool_items):
        logger.info(f"  {idx+1}. ID: {item.get('_id')}, State: {item.get('state')}, Status: {item.get('status')}")
        logger.info(f"     Content: {item.get('content', {}).get('raw_content', '')[:50]}...")
    
    # Get scheduled operations
    schedules = await db.scheduled_operations.find({"session_id": session_id}).to_list(None)
    logger.info(f"Scheduled Operations ({len(schedules)}):")
    for idx, schedule in enumerate(schedules):
        logger.info(f"  {idx+1}. ID: {schedule.get('_id')}")
        logger.info(f"     State: {schedule.get('schedule_state')}")
        logger.info(f"     Items: {len(schedule.get('pending_items', []))} pending, "
                   f"{len(schedule.get('approved_items', []))} approved")
    
    logger.info("=== END DATABASE STATE ===")

@pytest.mark.asyncio
async def test_tool_operation_flow(rin_agent_with_mocks):
    """Test complete tool operation flow through agent and orchestrator"""
    # Properly await the fixture
    agent = await rin_agent_with_mocks
    session_id = str(ObjectId())
    
    # Initialize session
    await agent.start_new_session(session_id)
    
    # Verify initial state
    assert agent.state_manager.current_state == AgentState.NORMAL_CHAT
    
    # Step 1: Trigger tool operation
    message = "schedule three tweets about the latest news in crypto"
    response = await agent.get_response(
        session_id=session_id,
        message=message,
        role="user"
    )
    
    # Log the flow for debugging
    logger.info(f"Response received: {response}")
    logger.info(f"Current state: {agent.state_manager.current_state}")
    
    # Verify state transition to TOOL_OPERATION
    assert agent.state_manager.current_state == AgentState.TOOL_OPERATION
    
    # Check database for operation
    db = MongoManager.get_db()
    await dump_db_state(db, session_id)
    
    operation = await db.get_tool_operation_state(session_id)
    assert operation is not None, "No operation found in database"
    logger.info(f"Operation state: {operation.get('state')}")
    
    # Step 2: Generate tweets
    # The agent should now be generating tweets, which happens automatically
    # in the first step since we're using a real implementation
    
    # Step 3: Simulate approval response
    approval_message = "These look great, approve all of them"
    approval_response = await agent.get_response(
        session_id=session_id,
        message=approval_message,
        role="user"
    )
    logger.info(f"Approval response: {approval_response}")
    
    # Verify database state after approval
    await dump_db_state(db, session_id)
    
    operation = await db.get_tool_operation_state(session_id)
    logger.info(f"Operation state after approval: {operation.get('state')}")
    
    # Step 4: Verify tweets were scheduled
    tool_items = await db.tool_items.find({"session_id": session_id}).to_list(None)
    logger.info(f"Found {len(tool_items)} tool items")
    
    # Verify we have some tool items
    assert len(tool_items) > 0, "No tool items were created"
    
    # Verify at least some items reached the correct state
    completed_items = [item for item in tool_items if item.get('state') == ToolOperationState.COMPLETED.value]
    executing_items = [item for item in tool_items if item.get('state') == ToolOperationState.EXECUTING.value]
    scheduled_items = [item for item in tool_items if item.get('status') == OperationStatus.SCHEDULED.value]
    
    logger.info(f"Items by state: COMPLETED={len(completed_items)}, EXECUTING={len(executing_items)}, SCHEDULED={len(scheduled_items)}")
    assert len(completed_items) > 0 or len(executing_items) > 0 or len(scheduled_items) > 0, \
        "No items reached COMPLETED, EXECUTING, or SCHEDULED state"
        
    # Step 5: Verify final state returned to normal chat 
    # Depending on whether items are scheduled or completed immediately
    # The agent state might be different, so we check both possibilities
    assert agent.state_manager.current_state in [AgentState.NORMAL_CHAT, AgentState.TOOL_OPERATION], \
        f"Expected final state to be NORMAL_CHAT or TOOL_OPERATION, got {agent.state_manager.current_state}"
    
    # Final database state
    await dump_db_state(db, session_id)

@pytest.mark.asyncio
async def test_error_handling(rin_agent_with_mocks):
    """Test error handling in tool operations"""
    agent, mock_orchestrator = await rin_agent_with_mocks
    session_id = str(ObjectId())
    
    # Set up error condition
    error_message = "Tool execution failed"
    agent.state_manager.handle_agent_state = AsyncMock(side_effect=Exception(error_message))
    
    response = await agent.get_response(
        session_id=session_id,
        message="schedule tweets",
        role="user"
    )
    
    # Verify error handling
    assert isinstance(response, str)
    assert "technical difficulty" in response.lower()
    assert agent.state_manager.handle_agent_state.await_count == 1

@pytest.mark.asyncio
async def test_tweet_regeneration_flow(rin_agent_with_mocks):
    """Test the regeneration flow for tweets when user rejects them"""
    # Properly await the fixture
    agent = await rin_agent_with_mocks
    session_id = str(ObjectId())
    
    # Initialize session
    await agent.start_new_session(session_id)
    
    # Step 1: Trigger tool operation
    message = "schedule three tweets about bitcoin"
    response = await agent.get_response(
        session_id=session_id,
        message=message,
        role="user"
    )
    
    # Verify initial state
    db = MongoManager.get_db()
    await dump_db_state(db, session_id)
    
    # Step 2: Reject the tweets to trigger regeneration
    rejection_message = "These don't look good, please regenerate them"
    rejection_response = await agent.get_response(
        session_id=session_id,
        message=rejection_message,
        role="user"
    )
    logger.info(f"Rejection response: {rejection_response}")
    
    # Verify database state after rejection
    await dump_db_state(db, session_id)
    
    operation = await db.get_tool_operation_state(session_id)
    logger.info(f"Operation state after rejection: {operation.get('state')}")
    
    # We should be in COLLECTING state for regeneration
    assert operation.get('state') == ToolOperationState.COLLECTING.value, \
        f"Expected state to be COLLECTING, got {operation.get('state')}"
    
    # Step 3: Now approve the regenerated tweets
    approval_message = "These look much better, approve all"
    approval_response = await agent.get_response(
        session_id=session_id,
        message=approval_message,
        role="user"
    )
    logger.info(f"Approval response: {approval_response}")
    
    # Verify final state
    await dump_db_state(db, session_id)
    
    # Verify we have both rejected and approved items
    tool_items = await db.tool_items.find({"session_id": session_id}).to_list(None)
    
    rejected_items = [item for item in tool_items if item.get('status') == OperationStatus.REJECTED.value]
    approved_items = [item for item in tool_items 
                     if item.get('status') in [OperationStatus.APPROVED.value, OperationStatus.SCHEDULED.value]]
    
    logger.info(f"Found {len(rejected_items)} rejected items and {len(approved_items)} approved items")
    
    # Verify we have both rejected and approved items
    assert len(rejected_items) > 0, "No items were rejected"
    assert len(approved_items) > 0, "No items were approved"

@pytest.mark.asyncio
async def test_twitter_analyze_command(rin_agent_with_mocks):
    """Test that analyze_command in TwitterTool properly sets approval and scheduling flags"""
    # Properly await the fixture
    agent = await rin_agent_with_mocks
    session_id = str(ObjectId())
    
    # Get the TwitterTool instance
    twitter_tool = agent.orchestrator.tools.get(ToolType.TWITTER.value)
    assert twitter_tool is not None, "TwitterTool not registered"
    
    # Mock LLM response to simulate specific behavior
    original_get_response = twitter_tool.llm_service.get_response
    async def mock_get_response(prompt, model_type=None, override_config=None):
        # Check if this is the analyze_command prompt
        if isinstance(prompt, list) and len(prompt) > 1 and "Twitter action analyzer" in str(prompt[1].get('content', '')):
            # Return a fixed JSON response for scheduling tweets
            return """
            {
                "tools_needed": [{
                    "tool_name": "twitter",
                    "action": "schedule_items",
                    "parameters": {
                        "item_count": 3,
                        "topic": "bitcoin cryptocurrency trends",
                        "schedule_type": "one_time",
                        "schedule_time": "spread_24h",
                        "interval_minutes": 480,
                        "approval_required": true,
                        "schedule_required": true
                    },
                    "priority": 1
                }],
                "reasoning": "User requested scheduling tweets about bitcoin"
            }
            """
        # For all other prompts, use the original implementation
        return await original_get_response(prompt, model_type, override_config)
    
    # Apply the mock
    twitter_tool.llm_service.get_response = AsyncMock(side_effect=mock_get_response)
    
    try:
        # Set the session ID
        twitter_tool.deps.session_id = session_id
        
        # Call analyze_command directly
        message = "schedule three tweets about bitcoin"
        result = await twitter_tool._analyze_command(message)
        
        # Verify the result
        assert "tool_operation_id" in result, "Missing tool_operation_id in result"
        assert "schedule_id" in result, "Missing schedule_id in result"
        assert "topic" in result, "Missing topic in result"
        assert result["topic"] == "bitcoin cryptocurrency trends", f"Expected topic to be bitcoin cryptocurrency trends, got {result.get('topic')}"
        assert "item_count" in result, "Missing item_count in result"
        assert result["item_count"] == 3, f"Expected item_count to be 3, got {result.get('item_count')}"
        
        # Verify the operation in the database
        db = MongoManager.get_db()
        operation = await db.get_tool_operation_state(session_id)
        assert operation is not None, "No operation created in database"
        
        # Check initial data set in tool_state_manager.update_operation
        input_data = operation.get("input_data", {})
        command_info = input_data.get("command_info", {})
        
        # Check registry data
        registry_data = operation.get("input_data", {}).get("tool_registry", {})
        logger.info(f"Tool registry data: {registry_data}")
        assert registry_data.get("requires_approval") is True, "requires_approval not set correctly in registry data"
        assert registry_data.get("requires_scheduling") is True, "requires_scheduling not set correctly in registry data"
        assert registry_data.get("content_type") == ContentType.TWEET.value, f"content_type not set correctly: {registry_data.get('content_type')}"
        
        # Check command info data
        logger.info(f"Command info: {command_info}")
        assert command_info.get("topic") == "bitcoin cryptocurrency trends", f"topic not set correctly: {command_info.get('topic')}"
        assert command_info.get("item_count") == 3, f"item_count not set correctly: {command_info.get('item_count')}"
        assert command_info.get("schedule_type") == "one_time", f"schedule_type not set correctly: {command_info.get('schedule_type')}"
        assert command_info.get("schedule_time") == "spread_24h", f"schedule_time not set correctly: {command_info.get('schedule_time')}"
        
        # Check schedule metadata
        metadata = operation.get("metadata", {})
        logger.info(f"Operation metadata: {metadata}")
        assert metadata.get("schedule_state") == ScheduleState.PENDING.value, f"schedule_state not set correctly: {metadata.get('schedule_state')}"
        assert "schedule_id" in metadata, "schedule_id not set in metadata"
        assert metadata.get("schedule_id") == input_data.get("schedule_id"), "schedule_id mismatch"
        
        # Also verify schedule was created in database
        schedule_id = metadata.get("schedule_id")
        scheduled_op = await db.scheduled_operations.find_one({"_id": ObjectId(schedule_id)})
        assert scheduled_op is not None, f"No scheduled operation found with ID {schedule_id}"
        assert scheduled_op.get("schedule_state") == ScheduleState.PENDING.value, \
            f"Expected schedule_state to be PENDING, got {scheduled_op.get('schedule_state')}"
        assert scheduled_op.get("tool_operation_id") == result["tool_operation_id"], \
            "Schedule operation not linked to correct tool operation"
            
    finally:
        # Restore the original method
        twitter_tool.llm_service.get_response = original_get_response 