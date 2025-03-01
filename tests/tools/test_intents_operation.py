import pytest
import asyncio
import os
import sys
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch
from bson.objectid import ObjectId

# Add the src directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from tools.intents_operation import IntentsTool
from db.enums import OperationStatus, ToolOperationState, ScheduleState
from services.monitoring_service import LimitOrderMonitoringService
from clients.near_intents_client.intents_client import (
    IntentRequest,
    fetch_options,
    select_best_option
)

class TestIntentsTool:
    """Test class for IntentsTool functionality"""
    
    @pytest.fixture
    async def setup_tool(self):
        """Set up the IntentsTool with mocked dependencies"""
        # Create mock dependencies
        tool = IntentsTool()
        
        # Mock tool_state_manager
        tool.tool_state_manager = MagicMock()
        tool.tool_state_manager.get_operation = MagicMock(return_value={
            "_id": ObjectId(),
            "session_id": "test_session",
            "state": ToolOperationState.COLLECTING.value,
            "status": OperationStatus.PENDING.value,
            "output_data": {"pending_items": []}
        })
        
        # Mock LLM service
        tool.llm_service = MagicMock()
        tool.llm_service.get_response = MagicMock(return_value="""
        {
            "tools_needed": [{
                "tool_name": "intents",
                "action": "limit_order",
                "parameters": {
                    "from_token": "NEAR",
                    "from_amount": 5.0,
                    "to_token": "USDC",
                    "min_price": 3.0,
                    "to_chain": "eth",
                    "expiration_hours": 24,
                    "slippage": 0.5
                },
                "priority": 1
            }],
            "reasoning": "User requested a limit order to swap 5 NEAR to USDC when the price reaches $3.00 per NEAR"
        }
        """)
        
        # Mock schedule_manager
        tool.schedule_manager = MagicMock()
        tool.schedule_manager.initialize_schedule = MagicMock(return_value="test_schedule_id")
        
        # Mock database
        tool.db = MagicMock()
        tool.db.tool_items = MagicMock()
        tool.db.tool_items.insert_one = MagicMock(return_value=MagicMock(inserted_id=ObjectId()))
        
        # Mock solver_bus_client
        tool.solver_bus_client = MagicMock()
        
        # Mock near_account
        tool.near_account = MagicMock()
        
        return tool
    
    @pytest.mark.asyncio
    async def test_analyze_command_limit_order(self, setup_tool):
        """Test analyzing a limit order command"""
        tool = await setup_tool
        
        # Test command
        command = "I want to swap 5 NEAR for USDC at $3.00 / NEAR"
        
        # Call analyze_command
        result = await tool._analyze_command(command)
        
        # Verify result
        assert result["operation_type"] == "limit_order"
        assert result["parameters"]["from_token"] == "NEAR"
        assert result["parameters"]["from_amount"] == 5.0
        assert result["parameters"]["to_token"] == "USDC"
        assert result["parameters"]["min_price"] == 3.0
        assert "monitoring_params" in result
        assert "schedule_id" in result
        
        # Verify schedule creation
        tool.schedule_manager.initialize_schedule.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_generate_content_limit_order(self, setup_tool):
        """Test generating content for a limit order"""
        tool = await setup_tool
        
        # Create operation info
        operation_info = {
            "tool_operation_id": str(ObjectId()),
            "operation_type": "limit_order",
            "parameters": {
                "from_token": "NEAR",
                "from_amount": 5.0,
                "to_token": "USDC",
                "min_price": 3.0,
                "to_chain": "eth",
                "expiration_hours": 24,
                "slippage": 0.5
            }
        }
        
        # Mock current price fetching
        with patch('clients.near_intents_client.intents_client.fetch_options') as mock_fetch:
            mock_fetch.return_value = [{
                "amount_in": str(5 * 10**24),  # 5 NEAR in base units
                "amount_out": str(10 * 10**6),  # 10 USDC in base units
                "quote_hash": "test_hash"
            }]
            
            # Call generate_content
            result = await tool._generate_content(operation_info)
        
        # Verify result
        assert result["success"] is True
        assert "items" in result
        assert len(result["items"]) > 0
        assert result["operation_type"] == "limit_order"
        assert result["requires_approval"] is True
        
        # Verify item creation
        tool.db.tool_items.insert_one.assert_called()
    
    @pytest.mark.asyncio
    async def test_execute_approved_items(self, setup_tool):
        """Test executing approved items"""
        tool = await setup_tool
        
        # Create mock operation
        operation = {
            "_id": ObjectId(),
            "session_id": "test_session",
            "state": ToolOperationState.EXECUTING.value,
            "status": OperationStatus.APPROVED.value
        }
        
        # Mock get_operation_items to return approved items
        tool.tool_state_manager.get_operation_items = MagicMock(return_value=[
            {
                "_id": ObjectId(),
                "content": {
                    "operation_type": "deposit",
                    "token_symbol": "NEAR",
                    "amount": 5.0
                }
            },
            {
                "_id": ObjectId(),
                "content": {
                    "operation_type": "swap",
                    "from_token": "NEAR",
                    "from_amount": 5.0,
                    "to_token": "USDC",
                    "to_chain": "eth"
                }
            }
        ])
        
        # Mock intent_deposit and intent_swap
        with patch('tools.intents_operation.intent_deposit') as mock_deposit, \
             patch('tools.intents_operation.intent_swap') as mock_swap, \
             patch('tools.intents_operation.wrap_near') as mock_wrap:
            
            mock_deposit.return_value = {"success": True}
            mock_swap.return_value = {"success": True, "amount_out": "10000000"}  # 10 USDC
            mock_wrap.return_value = {"success": True}
            
            # Call execute_approved_items
            result = await tool.execute_approved_items(operation)
        
        # Verify result
        assert result["status"] == "completed"
        assert "Successfully executed operations" in result["response"]
        assert result["state"] == ToolOperationState.COMPLETED.value
        
        # Verify function calls
        mock_deposit.assert_called_once()
        mock_swap.assert_called_once()
        tool.db.tool_items.update_one.assert_called()
        tool.tool_state_manager.update_operation.assert_called()

    @pytest.mark.asyncio
    async def test_limit_order_monitoring_integration(self, setup_tool):
        """Test limit order monitoring integration"""
        tool = await setup_tool
        
        # Create a monitoring service mock
        monitoring_service = MagicMock(spec=LimitOrderMonitoringService)
        monitoring_service.register_limit_order = MagicMock(return_value=True)
        monitoring_service.start = MagicMock()
        monitoring_service.stop = MagicMock()
        
        # Create a limit order
        command = "I want to swap 5 NEAR for USDC at $3.00 / NEAR"
        result = await tool._analyze_command(command)
        
        # Verify monitoring parameters
        assert "monitoring_params" in result
        assert "check_interval_seconds" in result["monitoring_params"]
        assert "expiration_timestamp" in result["monitoring_params"]
        
        # Simulate monitoring service registration
        order_id = str(ObjectId())
        registration_result = await monitoring_service.register_limit_order(
            order_id, 
            result["monitoring_params"]
        )
        
        assert registration_result is True
        monitoring_service.register_limit_order.assert_called_once()