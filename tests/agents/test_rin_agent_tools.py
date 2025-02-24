import pytest
from datetime import datetime, UTC
from src.agents.rin.agent import RinAgent
from src.db.enums import AgentState, ToolOperationState, ContentType, ToolType
from src.tools.base import AgentResult, AgentDependencies, ToolRegistry
from src.utils.trigger_detector import TriggerDetector
from src.db.mongo_manager import MongoManager
from src.managers.schedule_manager import ScheduleManager
from src.db.db_schema import RinDB
from unittest.mock import Mock, AsyncMock, patch
import os
import json
from pathlib import Path
from dotenv import load_dotenv
import logging
import time
from bson import ObjectId
from src.managers.tool_state_manager import ToolStateManager
from src.services.schedule_service import ScheduleService
from src.managers.agent_state_manager import AgentStateManager
import asyncio
from typing import Dict

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()

def load_config():
    """Load configuration from config.json"""
    current_dir = Path(__file__).resolve()
    project_root = current_dir.parent.parent.parent
    config_path = project_root / 'src' / 'config' / 'config.json'
    
    with open(config_path) as f:
        config_str = f.read()
        for key, value in os.environ.items():
            if value is not None:
                config_str = config_str.replace(f"${{{key}}}", value)
        return json.loads(config_str)

class MockContextManager:
    def __init__(self, db):
        self.db = db
        
    async def initialize(self):
        pass
        
    async def store_interaction(self, *args, **kwargs):
        return True
        
    async def get_combined_context(self, *args, **kwargs):
        return []
        
    async def is_initialized(self):
        return True

class MockToolRegistry:
    def __init__(self):
        self.tools = {}
        
    def register_tool(self, tool_type: str, tool):
        self.tools[tool_type] = tool

class MockStateManager:
    def __init__(self, tool_state_manager, orchestrator, trigger_detector):
        self.current_state = AgentState.NORMAL_CHAT
        self.tool_state_manager = tool_state_manager
        self.orchestrator = orchestrator
        self.trigger_detector = trigger_detector

    async def handle_agent_state(self, message: str, session_id: str) -> Dict:
        """Mock state handling with proper state transitions"""
        if self.current_state == AgentState.NORMAL_CHAT:
            tool_type = self.trigger_detector.get_specific_tool_type(message)
            if tool_type:
                self.current_state = AgentState.TOOL_OPERATION
                return {
                    "state": AgentState.TOOL_OPERATION.value,
                    "response": "Tool operation started",
                    "requires_approval": True,
                    "tool_type": tool_type
                }
        
        elif self.current_state == AgentState.TOOL_OPERATION:
            result = await self.orchestrator.handle_tool_operation(
                message=message,
                session_id=session_id
            )
            
            if result.get("state") == ToolOperationState.COMPLETED.value:
                self.current_state = AgentState.NORMAL_CHAT
            
            return result

        return {
            "state": self.current_state.value,
            "status": "normal_chat"
        }

class MockToolStateManager:
    async def get_operation(self, session_id: str):
        return {
            "state": ToolOperationState.COLLECTING.value,
            "tool_type": "twitter",
            "step": "initial"
        }
        
    async def end_operation(self, session_id: str, success: bool, api_response: dict = None):
        return True

class MockScheduleService:
    """Mock ScheduleService for testing"""
    def __init__(self, mongo_uri: str):
        self.running = False
        self._stop_event = asyncio.Event()
        
    async def start(self):
        self.running = True
        self._stop_event.clear()
        
    async def stop(self):
        self.running = False
        self._stop_event.set()
        
    async def _schedule_loop(self):
        """Mock the schedule loop to prevent 'str' has no attribute 'get' error"""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in mock schedule loop: {e}")

@pytest.fixture(autouse=True)
async def setup_teardown():
    """Setup and teardown for all tests"""
    try:
        yield
    finally:
        # Cleanup MongoDB collections
        try:
            db = MongoManager.get_db()
            if db:
                await db.scheduled_operations.delete_many({})
                await db.tool_operations.delete_many({})
                await db.tool_items.delete_many({})
        except Exception as e:
            logger.error(f"Error cleaning up database: {e}")
            
        # Ensure all schedule service tasks are stopped
        try:
            tasks = [t for t in asyncio.all_tasks() if 'schedule_loop' in str(t)]
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            logger.error(f"Error cleaning up schedule tasks: {e}")

@pytest.fixture
async def rin_agent():
    """Create and initialize RinAgent"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    
    try:
        # Create agent with mongo_uri
        agent = RinAgent(mongo_uri=mongo_uri)
        
        # Create test session
        test_session_id = f"test_session_{int(time.time())}"
        
        # Set up dependencies with session_id BEFORE initialization
        agent.deps = AgentDependencies(session_id=test_session_id)
        
        # Initialize the agent after setting deps
        await agent.initialize()
        
        # Mock components after initialization
        agent.trigger_detector = Mock(spec=TriggerDetector)
        agent.trigger_detector.should_use_tools = Mock(return_value=True)
        agent.trigger_detector.get_specific_tool_type = Mock(return_value="twitter")
        agent.trigger_detector.get_tool_operation_type = Mock(return_value="schedule_tweets")
        agent.trigger_detector.should_use_twitter = Mock(return_value=True)
        
        # Mock orchestrator with proper dict responses
        agent.orchestrator = AsyncMock()
        # For run_tool in start_tool_operation
        agent.orchestrator.run_tool.return_value = {
            "response": "Tool operation started",
            "data": {
                "tool_type": "twitter",
                "status": "started"
            }
        }
        # For process_command in handle_message
        agent.orchestrator.process_command.return_value = {
            "response": "Tool execution in progress",
            "data": {
                "status": "in_progress",
                "tool_type": "twitter",
                "requires_input": True
            }
        }
        
        # Mock schedule service
        agent.schedule_service = MockScheduleService(mongo_uri)
        await agent.schedule_service.start()
        
        # Create fresh instances of managers
        agent.tool_state_manager = ToolStateManager(
            db=MongoManager.get_db(),
            schedule_service=agent.schedule_service
        )
        
        # Create state manager with proper mocks
        agent.state_manager = AgentStateManager(
            tool_state_manager=agent.tool_state_manager,
            orchestrator=agent.orchestrator,
            trigger_detector=agent.trigger_detector
        )
        
        # Ensure session_id is accessible
        agent.session_id = test_session_id
        
        return agent
        
    except Exception as e:
        logger.error(f"Error in rin_agent fixture: {e}")
        await MongoManager.close()
        raise

@pytest.fixture
async def mock_trigger_detector(self):
    detector = Mock(spec=TriggerDetector)
    detector.should_use_tools = Mock(return_value=False)
    detector.get_specific_tool_type = Mock(return_value=None)
    detector.get_tool_operation_type = Mock(return_value=None)
    detector.should_use_twitter = Mock(return_value=False)
    return detector

@pytest.fixture
async def mock_orchestrator(self):
    orchestrator = AsyncMock()
    orchestrator.initialize = AsyncMock()
    orchestrator.process_command = AsyncMock()
    orchestrator.tool_registry = MockToolRegistry()
    return orchestrator

@pytest.fixture
async def mock_schedule_manager(self):
    manager = Mock(spec=ScheduleManager)
    manager.tool_registry = MockToolRegistry()
    return manager


@pytest.mark.asyncio
async def test_active_tool_operation(rin_agent):
    """Test handling of active tool operation"""
    agent = await rin_agent
    test_session_id = agent.deps.session_id
    
    # Mock state manager response for tool trigger
    state_result = {
        "state": AgentState.TOOL_OPERATION.value,
        "response": "Tool operation started",
        "requires_approval": True,
        "tool_type": ToolType.TWITTER.value
    }
    agent.state_manager.handle_agent_state = AsyncMock()
    agent.state_manager.handle_agent_state.return_value = state_result
    
    # First message to trigger tool operation
    trigger_message = "schedule some tweets"
    first_response = await agent.get_response(
        session_id=test_session_id,
        message=trigger_message,
        role="host"
    )
    
    # Verify state transition
    assert agent.state_manager.current_state == AgentState.TOOL_OPERATION
    assert "Tool operation started" in first_response["response"]

@pytest.mark.asyncio
async def test_tool_operation_completion(rin_agent, mocker):
    """Test successful completion of tool operation"""
    agent = await rin_agent
    test_session_id = agent.deps.session_id
    
    # Setup initial state
    agent.state_manager.current_state = AgentState.TOOL_OPERATION
    
    # Mock state manager response for completion
    completion_result = {
        "state": AgentState.NORMAL_CHAT.value,
        "response": "Operation completed successfully",
        "tool_results": {
            "status": "completed",
            "tool_type": ToolType.TWITTER.value
        }
    }
    agent.state_manager.handle_agent_state = AsyncMock(return_value=completion_result)
    
    # Test completion message
    message = "done"
    response = await agent.get_response(
        session_id=test_session_id,
        message=message,
        role="host"
    )
    
    # Verify state transition and response
    assert agent.state_manager.current_state == AgentState.NORMAL_CHAT
    assert "completed" in response["response"].lower()

@pytest.mark.asyncio
async def test_scheduled_operation(rin_agent):
    """Test handling of scheduled operations"""
    agent = await rin_agent
    
    # Verify schedule service was started
    assert agent.schedule_service.running == True
    
    # Setup trigger detection for scheduling
    agent.trigger_detector.should_use_tools.return_value = True
    agent.trigger_detector.get_specific_tool_type.return_value = "twitter"
    agent.trigger_detector.get_tool_operation_type.return_value = "schedule_tweets"
    
    # Setup orchestrator response for scheduling
    agent.orchestrator.run_tool.return_value = {
        "response": "Operation scheduled",
        "data": {
            "completion_type": "scheduled",
            "schedule_id": "sched_123",
            "tool_type": "twitter",
            "status": "scheduled"
        }
    }
    
    # Test scheduling message
    message = "schedule tweets for tomorrow"
    response = await agent.process_message(message=message, author="test_user")
    
    assert "scheduled" in response["response"].lower()
    assert response["data"]["schedule_id"] == "sched_123"
    assert response["data"]["status"] == "scheduled"
    
    # Verify schedule service is still running
    assert agent.schedule_service.running == True

@pytest.mark.asyncio
async def test_tweet_tool_lifecycle(rin_agent):
    """Test complete lifecycle of a tweet tool operation"""
    agent = await rin_agent
    test_session_id = agent.deps.session_id
    
    # 1. Initial Setup
    assert agent.state_manager.current_state == AgentState.NORMAL_CHAT
    
    # 2. Tool Trigger
    trigger_result = {
        "state": AgentState.TOOL_OPERATION.value,
        "response": "Starting tweet operation",
        "requires_approval": True,
        "tool_type": ToolType.TWITTER.value
    }
    agent.state_manager.handle_agent_state = AsyncMock(return_value=trigger_result)
    
    # Test trigger
    trigger_message = "schedule tweets for tomorrow about AI"
    first_response = await agent.get_response(
        session_id=test_session_id,
        message=trigger_message,
        role="user"
    )
    
    assert agent.state_manager.current_state == AgentState.TOOL_OPERATION
    
    # 3. Approval Flow
    approval_result = {
        "state": AgentState.TOOL_OPERATION.value,
        "response": "Please review generated tweets",
        "requires_approval": True,
        "items": [
            {"content": "Tweet 1", "id": "1"},
            {"content": "Tweet 2", "id": "2"}
        ]
    }
    agent.state_manager.handle_agent_state = AsyncMock(return_value=approval_result)
    
    # 4. Scheduling
    schedule_result = {
        "state": AgentState.NORMAL_CHAT.value,
        "response": "Tweets scheduled successfully",
        "schedule_id": "test_schedule_123",
        "tool_results": {
            "status": "scheduled",
            "completion_type": "scheduled"
        }
    }
    agent.state_manager.handle_agent_state = AsyncMock(return_value=schedule_result)
    
    # Verify final state
    assert agent.state_manager.current_state == AgentState.NORMAL_CHAT
    assert agent.schedule_service.running == True 