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

    async def handle_exit(self, session_id: str, message: str = "exit") -> Dict:
        """Handle exit operations across all components"""
        try:
            logger.info(f"Handling exit for session {session_id}")
            
            # Get current operation if exists
            operation = await self.tool_state_manager.get_operation(session_id)
            if not operation:
                logger.info("No active operation to exit")
                await self._transition_state(AgentAction.CANCEL_TOOL, "Exit requested - no active operation")
                return {
                    "state": self.current_state.value,
                    "status": "exit",
                    "response": "Returning to normal chat.",
                    "requires_chat_response": True
                }

            # Get operation details
            tool_type = operation.get('tool_type')
            operation_id = str(operation['_id'])
            
            # Handle exit through orchestrator
            exit_result = await self.orchestrator.handle_tool_operation(
                message=message,
                session_id=session_id,
                tool_type=tool_type
            )
            
            # End operation with appropriate status
            await self.tool_state_manager.end_operation(
                session_id=session_id,
                tool_operation_id=operation_id,
                success=False,
                step="cancelled",
                api_response={
                    "status": "cancelled",
                    "reason": "User requested exit"
                }
            )
            
            # Transition state
            await self._transition_state(AgentAction.CANCEL_TOOL, "User requested exit")
            self._current_tool_type = None
            
            # Return result for chat generation
            return {
                "state": self.current_state.value,
                "status": "exit",
                "response": exit_result.get("response", "Operation cancelled. What would you like to do?"),
                "requires_chat_response": True,
                "operation_summary": {
                    "summary": "Operation was cancelled at your request.",
                    "operation_id": operation_id,
                    "status": "cancelled",
                    "tool_type": tool_type
                }
            }

        except Exception as e:
            logger.error(f"Error handling exit: {e}")
            await self._transition_state(AgentAction.ERROR, str(e))
            self._current_tool_type = None
            return self._create_error_response(str(e))

    async def handle_agent_state(self, message: str, session_id: str) -> Dict:
        """Main state handling method"""
        try:
            if not message:
                return self._create_error_response("Invalid message received")

            # Handle exit commands globally
            if message.lower() in ["exit", "quit", "cancel", "stop"]:
                return await self.handle_exit(session_id, message)

            # Store initial state
            initial_state = self.current_state
            logger.info(f"Current state before handling: {self.current_state}")

            # Get current operation
            operation = await self.tool_state_manager.get_operation(session_id)

            # NORMAL_CHAT: Check for tool triggers
            if self.current_state == AgentState.NORMAL_CHAT:
                tool_type = self.trigger_detector.get_specific_tool_type(message)
                if tool_type:
                    try:
                        # Transition to TOOL_OPERATION state BEFORE handling operation
                        await self._transition_state(
                            AgentAction.START_TOOL,
                            f"Starting {tool_type} operation"
                        )
                        
                        # Store tool_type for the session
                        self._current_tool_type = tool_type
                        logger.info(f"Starting tool operation with type: {tool_type}")

                        # Now handle the tool operation
                        result = await self.orchestrator.handle_tool_operation(
                            message=message,
                            session_id=session_id,
                            tool_type=tool_type
                        )
                        
                        if isinstance(result, dict):
                            return {
                                **result,
                                "state": self.current_state.value,
                                "tool_type": tool_type
                            }
                    except Exception as e:
                        logger.error(f"Error starting tool operation: {e}")
                        # Don't transition state on error - let approval_manager handle it
                        return self._create_error_response(str(e))

            # TOOL_OPERATION: Handle ongoing operation
            elif self.current_state == AgentState.TOOL_OPERATION:
                # Check if operation is in terminal state
                if operation and operation.get("state") in [
                    ToolOperationState.COMPLETED.value,
                    ToolOperationState.CANCELLED.value,
                    ToolOperationState.ERROR.value
                ]:
                    # Reset state to NORMAL_CHAT
                    await self._transition_state(
                        AgentAction.COMPLETE_TOOL,
                        "Previous operation completed"
                    )
                    self._current_tool_type = None

                    # Check for new tool trigger
                    tool_type = self.trigger_detector.get_specific_tool_type(message)
                    if tool_type:
                        # Start new operation
                        await self._transition_state(
                            AgentAction.START_TOOL,
                            f"Starting new {tool_type} operation"
                        )
                        self._current_tool_type = tool_type
                        
                        # Handle new operation
                        result = await self.orchestrator.handle_tool_operation(
                            message=message,
                            session_id=session_id,
                            tool_type=tool_type
                        )
                        
                        if isinstance(result, dict):
                            return {
                                **result,
                                "state": self.current_state.value,
                                "tool_type": tool_type
                            }
                else:
                    # Handle ongoing operation normally
                    try:
                        result = await self.orchestrator.handle_tool_operation(
                            message=message,
                            session_id=session_id,
                            tool_type=self._current_tool_type
                        )
                        
                        if isinstance(result, dict):
                            operation_status = result.get("status", "").lower()
                            
                            # Handle operation completion
                            if operation_status in ["completed", "cancelled", "error", "exit"]:
                                # Get operation summary if available
                                operation_summary = result.get("operation_summary", {})
                                
                                # Transition state based on status
                                action = AgentAction.COMPLETE_TOOL if operation_status == "completed" else AgentAction.CANCEL_TOOL
                                await self._transition_state(action, f"Operation {operation_status}")
                                
                                # Clear tool type as operation is complete
                                self._current_tool_type = None
                                
                                # Return with summary for chat context
                                return {
                                    "state": self.current_state.value,
                                    "status": operation_status,
                                    "requires_chat_response": True,
                                    "operation_summary": operation_summary,
                                    "response": operation_summary.get("summary") if operation_summary else result.get("response")
                                }
                            
                            # For ongoing operations
                            return {
                                **result,
                                "state": self.current_state.value,
                                "tool_type": self._current_tool_type
                            }

                    except Exception as e:
                        logger.error(f"Error in tool operation: {e}")
                        await self._transition_state(AgentAction.ERROR, str(e))
                        return self._create_error_response(str(e))

            # Default response for NORMAL_CHAT
            return {
                "state": self.current_state.value,
                "status": "normal_chat"
            }

        except Exception as e:
            logger.error(f"Error in state management: {e}")
            # Don't transition state here - let approval_manager handle it
            return self._create_error_response(str(e))

    def _create_error_response(self, error_message: str) -> Dict:
        """Create standardized error response"""
        return {
            "state": self.current_state.value,
            "error": error_message,
            "status": "error"
        } 