import pytest
import os
import sys
from pathlib import Path
from datetime import datetime, UTC
import logging
import asyncio
from bson.objectid import ObjectId
from unittest.mock import Mock, AsyncMock
from src.db.enums import AgentState, ToolOperationState, ContentType, ToolType
from typing import Dict, Any

# Add project root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

# Load environment variables before imports
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(project_root) / '.env')

# Import project modules
from src.managers.agent_state_manager import AgentStateManager, AgentAction
from src.managers.tool_state_manager import ToolStateManager
from src.tools.orchestrator import Orchestrator
from src.utils.trigger_detector import TriggerDetector
from src.tools.base import AgentDependencies, BaseTool, ToolRegistry, AgentResult
from src.db.mongo_manager import MongoManager

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

class MockTool(BaseTool):
    """Mock tool implementing BaseTool interface"""
    name = "twitter"
    description = "Mock Twitter tool for testing"
    version = "1.0.0"
    registry = ToolRegistry(
        content_type=ContentType.TWEET,
        tool_type=ToolType.TWITTER,
        requires_approval=True,
        requires_scheduling=True,
        required_managers=["tool_state_manager", "schedule_manager"]
    )

    def __init__(self):
        super().__init__()
        
    async def run(self, input_data: Any) -> Dict[str, Any]:
        return {
            "status": "completed",
            "response": "Tool execution completed",
            "requires_input": False
        }
    
    def can_handle(self, input_data: Any) -> bool:
        return True

class MockOrchestrator:
    """Mock orchestrator matching real Orchestrator interface"""
    def __init__(self):
        self.tools = {}
        self.register_tool(MockTool())
        
    async def process_command(self, command: str, deps) -> AgentResult:
        tool = self.tools.get("twitter")
        result = await tool.run(command)
        
        return AgentResult(
            response=result.get("response", "Command processed"),
            data={
                "status": "completed",
                "tool_type": "twitter",
                "requires_input": False
            }
        )

    async def run_tool(self, tool_type: str, input_data: str, session_id: str, operation_id: str) -> Dict:
        tool = self.tools.get(tool_type)
        if not tool:
            raise ValueError(f"Tool {tool_type} not found")
            
        result = await tool.run(input_data)
        return {
            "response": result.get("response", "Tool executed"),
            "data": {
                "status": "completed",
                "tool_type": tool_type
            }
        }

    def register_tool(self, tool: BaseTool):
        """Register tool using BaseTool interface"""
        self.tools[tool.name] = tool
        registry = tool.get_registry()
        logger.info(f"Registered mock tool: {tool.name} with registry: {registry}")

@pytest.fixture
def mock_tool_state_manager(mongo_connection):
    """Create mock tool state manager"""
    manager = Mock()
    manager.start_operation = AsyncMock(return_value={
        "_id": "test_operation_id",
        "state": ToolOperationState.COLLECTING.value
    })
    manager.get_operation = AsyncMock(return_value=None)
    return manager

@pytest.fixture
def mock_trigger_detector():
    """Create mock trigger detector"""
    detector = Mock()
    detector.get_specific_tool_type = Mock(return_value="twitter")
    return detector

@pytest.fixture
def agent_state_manager(mock_tool_state_manager, mock_trigger_detector):
    """Create AgentStateManager instance with mocks"""
    orchestrator = MockOrchestrator()
    return AgentStateManager(
        tool_state_manager=mock_tool_state_manager,
        orchestrator=orchestrator,
        trigger_detector=mock_trigger_detector
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
    finally:
        await MongoManager.close()

@pytest.fixture
def mock_dependencies():
    """Create mock dependencies for AgentStateManager"""
    # Mock tool state manager
    tool_state_manager = AsyncMock()
    tool_state_manager.get_operation = AsyncMock(return_value=None)
    
    # Mock orchestrator with proper tool operation handling
    orchestrator = AsyncMock()
    orchestrator.handle_tool_operation = AsyncMock(return_value={
        "status": "processing",
        "state": ToolOperationState.COLLECTING.value,
        "response": "Processing tool operation"
    })
    
    # Mock trigger detector with specific tool detection
    trigger_detector = Mock()
    trigger_detector.get_specific_tool_type = Mock(return_value="twitter")
    
    return tool_state_manager, orchestrator, trigger_detector

@pytest.mark.asyncio
async def test_initial_state(mock_dependencies):
    """Test initial state is NORMAL_CHAT"""
    tool_state_manager, orchestrator, trigger_detector = mock_dependencies
    manager = AgentStateManager(tool_state_manager, orchestrator, trigger_detector)
    assert manager.current_state == AgentState.NORMAL_CHAT

@pytest.mark.asyncio
async def test_start_tool_transition(mock_dependencies):
    """Test transition from NORMAL_CHAT to TOOL_OPERATION"""
    tool_state_manager, orchestrator, trigger_detector = mock_dependencies
    manager = AgentStateManager(tool_state_manager, orchestrator, trigger_detector)
    
    # Configure mock for successful tool start
    orchestrator.handle_tool_operation.return_value = {
        "status": "processing",
        "state": ToolOperationState.COLLECTING.value,
        "response": "Starting tool operation"
    }
    
    result = await manager.handle_agent_state(
        message="schedule tweets",
        session_id="test_session"
    )
    
    # Verify state transition
    assert manager.current_state == AgentState.TOOL_OPERATION
    assert result["state"] == AgentState.TOOL_OPERATION.value
    assert "Starting tool operation" in result["response"]

@pytest.mark.asyncio
async def test_complete_tool_transition(mock_dependencies):
    """Test transition from TOOL_OPERATION back to NORMAL_CHAT"""
    tool_state_manager, orchestrator, trigger_detector = mock_dependencies
    manager = AgentStateManager(tool_state_manager, orchestrator, trigger_detector)
    
    # Set initial state to TOOL_OPERATION
    await manager._transition_state(AgentAction.START_TOOL)
    
    # Configure mock for completion
    orchestrator.handle_tool_operation.return_value = {
        "status": "completed",
        "state": ToolOperationState.COMPLETED.value,
        "response": "Operation completed successfully"
    }
    
    result = await manager.handle_agent_state(
        message="confirm",
        session_id="test_session"
    )
    
    # Verify state transition
    assert manager.current_state == AgentState.NORMAL_CHAT
    assert result["state"] == AgentState.NORMAL_CHAT.value
    assert "completed" in result["status"].lower()

@pytest.mark.asyncio
async def test_error_handling(mock_dependencies):
    """Test error handling and state transitions"""
    tool_state_manager, orchestrator, trigger_detector = mock_dependencies
    manager = AgentStateManager(tool_state_manager, orchestrator, trigger_detector)
    
    # Test error during tool operation
    orchestrator.handle_tool_operation.side_effect = Exception("Test error")
    
    result = await manager.handle_agent_state(
        message="schedule tweets",
        session_id="test_session"
    )
    
    # Verify error response
    assert result["status"] == "error"
    assert "Test error" in result["error"]
    assert manager.current_state == AgentState.NORMAL_CHAT

@pytest.mark.asyncio
async def test_invalid_state_transition(mock_dependencies):
    """Test handling of invalid state transitions"""
    tool_state_manager, orchestrator, trigger_detector = mock_dependencies
    manager = AgentStateManager(tool_state_manager, orchestrator, trigger_detector)
    
    # Try invalid transition
    success = await manager._transition_state(AgentAction.COMPLETE_TOOL)
    
    # Verify transition was rejected
    assert not success
    assert manager.current_state == AgentState.NORMAL_CHAT

if __name__ == "__main__":
    pytest.main(["-v", "test_agent_state_manager.py"]) 