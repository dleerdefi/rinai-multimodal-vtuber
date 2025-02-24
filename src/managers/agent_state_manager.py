import logging
from typing import Optional, Dict
from src.db.enums import AgentState, ToolOperationState, ContentType
from src.tools.base import AgentDependencies, ToolRegistry, AgentResult
from enum import Enum

logger = logging.getLogger(__name__)

class AgentAction(Enum):
    """Actions that trigger agent state transitions"""
    START_TOOL = "start_tool"         # NORMAL_CHAT -> TOOL_OPERATION
    COMPLETE_TOOL = "complete_tool"   # TOOL_OPERATION -> NORMAL_CHAT
    CANCEL_TOOL = "cancel_tool"       # TOOL_OPERATION -> NORMAL_CHAT
    ERROR = "error"                   # Any -> NORMAL_CHAT

class AgentStateManager:
    def __init__(self, tool_state_manager, orchestrator, trigger_detector):
        self.current_state = AgentState.NORMAL_CHAT
        self.tool_state_manager = tool_state_manager
        self.orchestrator = orchestrator
        self.trigger_detector = trigger_detector
        self.active_operation = None
        self._current_tool_type = None  # Add tool type tracking
        
        # Define valid state transitions
        self.state_transitions = {
            (AgentState.NORMAL_CHAT, AgentAction.START_TOOL): AgentState.TOOL_OPERATION,
            (AgentState.TOOL_OPERATION, AgentAction.COMPLETE_TOOL): AgentState.NORMAL_CHAT,
            (AgentState.TOOL_OPERATION, AgentAction.CANCEL_TOOL): AgentState.NORMAL_CHAT,
            (AgentState.TOOL_OPERATION, AgentAction.ERROR): AgentState.NORMAL_CHAT,
            (AgentState.NORMAL_CHAT, AgentAction.ERROR): AgentState.NORMAL_CHAT,
        }

    async def _transition_state(self, action: AgentAction, reason: str = "") -> bool:
        """Handle state transitions with validation"""
        next_state = self.state_transitions.get((self.current_state, action))
        if next_state is None:
            logger.warning(f"Invalid state transition: {self.current_state} -> {action}")
            return False
            
        logger.info(f"State transition: {self.current_state} -> {next_state} ({reason})")
        self.current_state = next_state
        return True

    async def handle_agent_state(self, message: str, session_id: str) -> Dict:
        """Main state handling method"""
        try:
            if not message:
                return self._create_error_response("Invalid message received")

            # Store initial state
            initial_state = self.current_state
            logger.info(f"Current state before handling: {self.current_state}")

            # NORMAL_CHAT: Check for tool triggers
            if self.current_state == AgentState.NORMAL_CHAT:
                tool_type = self.trigger_detector.get_specific_tool_type(message)
                if tool_type:
                    try:
                        # Store tool_type for the session
                        self._current_tool_type = tool_type
                        logger.info(f"Starting tool operation with type: {tool_type}")

                        # Start tool operation with explicit tool_type
                        result = await self.orchestrator.handle_tool_operation(
                            message=message,
                            session_id=session_id,
                            tool_type=tool_type  # Pass tool_type explicitly
                        )
                        
                        if isinstance(result, dict):
                            # Transition to TOOL_OPERATION state
                            await self._transition_state(
                                AgentAction.START_TOOL,
                                f"Starting {tool_type} operation"
                            )
                            
                            return {
                                **result,
                                "state": self.current_state.value,
                                "tool_type": tool_type
                            }
                    except Exception as e:
                        logger.error(f"Error starting tool operation: {e}")
                        raise

            # TOOL_OPERATION: Handle ongoing operation
            elif self.current_state == AgentState.TOOL_OPERATION:
                # Pass the stored tool_type for ongoing operations
                result = await self.orchestrator.handle_tool_operation(
                    message=message,
                    session_id=session_id,
                    tool_type=self._current_tool_type  # Pass stored tool_type
                )
                
                if isinstance(result, dict):
                    # Check operation completion status
                    operation_status = result.get("status", "").lower()
                    if operation_status in ["completed", "cancelled"]:
                        action = AgentAction.COMPLETE_TOOL if operation_status == "completed" else AgentAction.CANCEL_TOOL
                        await self._transition_state(action, f"Operation {operation_status}")
                        self._current_tool_type = None  # Clear tool_type on completion
                    elif operation_status == "error":
                        await self._transition_state(AgentAction.ERROR, "Operation error")
                        self._current_tool_type = None  # Clear tool_type on error
                    
                    return {
                        **result,
                        "state": self.current_state.value,
                        "response": result.get("response", "Processing your request...")
                    }

            # Default response for NORMAL_CHAT
            return {
                "state": self.current_state.value,
                "status": "normal_chat"
            }

        except Exception as e:
            logger.error(f"Error in state management: {e}")
            await self._transition_state(AgentAction.ERROR, str(e))
            self._current_tool_type = None  # Clear tool_type on error
            return self._create_error_response(str(e))

    def _create_error_response(self, error_message: str) -> Dict:
        """Create standardized error response"""
        return {
            "state": self.current_state.value,
            "error": error_message,
            "status": "error"
        } 