import pytest
from datetime import datetime, UTC
from src.agents.rin.agent import RinAgent
from src.tools.orchestrator import Orchestrator
from src.managers.tool_state_manager import ToolStateManager
from src.managers.agent_state_manager import AgentStateManager
from src.db.enums import (
    AgentState, 
    ToolOperationState, 
    OperationStatus, 
    ContentType, 
    ToolType
)
from src.tools.base import AgentResult, AgentDependencies
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from bson import ObjectId
import logging
from src.db.db_schema import RinDB
from src.db.mongo_manager import MongoManager
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Dict, Optional
from unittest.mock import call
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
        self.client = AsyncMock()
        self.db = AsyncMock()
        
        # Mock collection names and ping command
        self.db.list_collection_names = AsyncMock(return_value=[
            'rin.messages',
            'rin.context_configs',
            'rin.tool_items',
            'rin.tool_operations',
            'rin.tool_executions',
            'rin.scheduled_operations'
        ])
        self.db.command = AsyncMock(return_value={"ok": 1})
        
        # Initialize collections
        self.messages = MockCollection()
        self.context_configs = MockCollection()
        self.tool_items = MockCollection()
        self.tool_operations = MockCollection()
        self.tool_executions = MockCollection()
        self.scheduled_operations = MockCollection()
        self.tweets = MockCollection()
        self.tweet_schedules = MockCollection()
        
        self._initialized = False

    async def initialize(self):
        self._initialized = True
        return True
        
    async def is_initialized(self):
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

@pytest.fixture
async def mock_db():
    """Create mock database without real MongoDB connection"""
    db = MockDB()
    await db.initialize()
    return db

@pytest.fixture
async def rin_agent_with_mocks(mock_db):
    """Create RinAgent with mocked dependencies"""
    db = await mock_db
    
    with patch('src.db.mongo_manager.MongoManager.initialize', AsyncMock(return_value=None)), \
         patch('src.db.mongo_manager.MongoManager.get_db', return_value=db), \
         patch('src.db.db_schema.RinDB.initialize', AsyncMock(return_value=True)), \
         patch('motor.motor_asyncio.AsyncIOMotorClient', return_value=Mock()):
        
        agent = RinAgent(mongo_uri="mongodb://mock")
        
        # Mock trigger detector first
        mock_trigger_detector = Mock(spec=TriggerDetector)
        mock_trigger_detector.get_specific_tool_type = Mock(return_value=ToolType.TWITTER.value)
        mock_trigger_detector.detect_tool_trigger = Mock(return_value=True)
        mock_trigger_detector.should_use_twitter = Mock(return_value=True)
        agent.trigger_detector = mock_trigger_detector
        
        # Create state manager that properly propagates tool type
        mock_state_manager = Mock(spec=AgentStateManager)
        mock_state_manager.current_state = AgentState.NORMAL_CHAT
        mock_state_manager.trigger_detector = mock_trigger_detector
        
        async def mock_handle_agent_state(message: str, session_id: str):
            """Mock the state transition using trigger detector"""
            # Get tool type from trigger detector
            tool_type = mock_trigger_detector.get_specific_tool_type(message)
            if tool_type:
                mock_state_manager.current_state = AgentState.TOOL_OPERATION
                # Store tool_type for later use
                mock_state_manager._current_tool_type = tool_type
                return {
                    "state": AgentState.TOOL_OPERATION.value,
                    "status": "processing",
                    "response": f"Processing {tool_type} operation",
                    "tool_type": tool_type,
                    "data": {
                        "tool_type": tool_type,
                        "requires_approval": True,
                        "requires_scheduling": True
                    }
                }
            return {
                "state": mock_state_manager.current_state.value,
                "status": "normal_chat",
                "response": "Continuing chat"
            }
        
        # Add method to get current tool type
        mock_state_manager.get_current_tool_type = lambda: getattr(mock_state_manager, '_current_tool_type', None)
        mock_state_manager.handle_agent_state = AsyncMock(side_effect=mock_handle_agent_state)
        agent.state_manager = mock_state_manager

        # Create orchestrator that uses state manager's tool type
        mock_orchestrator = Mock(spec=Orchestrator)
        
        async def mock_handle_tool_operation(message: str, session_id: str, tool_type: Optional[str] = None):
            """Mock the tool operation handling"""
            # Get tool_type from state manager if not provided
            if not tool_type:
                tool_type = mock_state_manager.get_current_tool_type()
                logger.info(f"Retrieved tool type from state manager: {tool_type}")
            
            return {
                "response": f"Processing {tool_type} operation",
                "state": ToolOperationState.COLLECTING.value,
                "status": "processing",
                "tool_type": tool_type,
                "requires_approval": True,
                "requires_scheduling": True
            }
        
        mock_orchestrator.handle_tool_operation = AsyncMock(side_effect=mock_handle_tool_operation)
        mock_orchestrator.trigger_detector = mock_trigger_detector
        agent.orchestrator = mock_orchestrator

        # Mock tool state manager
        mock_tool_state_manager = Mock(spec=ToolStateManager)
        mock_tool_state_manager.initialize = AsyncMock()
        mock_tool_state_manager.db = db
        
        async def mock_get_operation(session_id: str):
            return None
            
        async def mock_start_operation(session_id: str, tool_type: str, initial_data: Optional[Dict] = None):
            return {
                "session_id": session_id,
                "tool_type": tool_type,
                "state": ToolOperationState.COLLECTING.value,
                "_id": ObjectId(),
                "metadata": initial_data or {}
            }
            
        mock_tool_state_manager.get_operation = AsyncMock(side_effect=mock_get_operation)
        mock_tool_state_manager.start_operation = AsyncMock(side_effect=mock_start_operation)
        agent.tool_state_manager = mock_tool_state_manager
        
        # Mock context manager
        mock_context = Mock(spec=RinContext)
        mock_context.initialize = AsyncMock()
        mock_context.store_interaction = AsyncMock()
        mock_context.get_combined_context = AsyncMock(return_value=[])
        mock_context.get_session_history = AsyncMock(return_value=[])
        mock_context.db = db
        agent.context_manager = mock_context
        
        # Initialize agent
        await agent.initialize()
        return agent, mock_orchestrator

@pytest.mark.asyncio
async def test_tool_operation_flow(rin_agent_with_mocks):
    """Test complete tool operation flow through agent and orchestrator"""
    agent, mock_orchestrator = await rin_agent_with_mocks
    session_id = str(ObjectId())
    
    # Initialize session
    await agent.start_new_session(session_id)
    
    # Verify initial state
    assert agent.state_manager.current_state == AgentState.NORMAL_CHAT
    
    # Trigger tool operation
    message = "schedule three tweets about the latest news in crypto"
    response = await agent.get_response(
        session_id=session_id,
        message=message,
        role="user"
    )
    
    # Log the flow for debugging
    logger.info(f"Response received: {response}")
    logger.info(f"Current state: {agent.state_manager.current_state}")
    logger.info(f"Tool operation calls: {mock_orchestrator.handle_tool_operation.call_args_list}")
    
    # Verify state transition
    assert agent.state_manager.current_state == AgentState.TOOL_OPERATION
    
    # Verify orchestrator interaction
    assert mock_orchestrator.handle_tool_operation.await_count == 1
    
    # Get and verify the tool operation call
    tool_call = mock_orchestrator.handle_tool_operation.call_args_list[0]
    assert "tool_type" in tool_call.kwargs, "tool_type missing from orchestrator call"
    assert tool_call.kwargs["tool_type"] == ToolType.TWITTER.value, \
        f"Expected tool_type to be {ToolType.TWITTER.value}, got {tool_call.kwargs.get('tool_type')}"

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