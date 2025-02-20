from typing import Dict, List, Callable, Optional, Any
from datetime import datetime, UTC
import logging
from enum import Enum
from bson.objectid import ObjectId
import json
from src.db.db_schema import (
    RinDB, 
    ToolOperation, 
    ToolOperationState, 
    OperationStatus,
    ContentType,
    ToolType
)
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService, ModelType
from pymongo import MongoClient
from src.services.approval_analyzer import ApprovalAnalyzer

logger = logging.getLogger(__name__)

class ApprovalAction(Enum):
    """User actions that trigger state transitions"""
    FULL_APPROVAL = "full_approval"
    PARTIAL_APPROVAL = "partial_approval"
    REGENERATE_ALL = "regenerate_all"
    AWAITING_INPUT = "awaiting_input"
    ERROR = "error"
    EXIT = "exit"

class ApprovalState(Enum):
    """Sub-states during the approval workflow"""
    AWAITING_INITIAL = "awaiting_initial"
    AWAITING_APPROVAL = "awaiting_approval"
    PARTIALLY_APPROVED = "partial_approval"
    REGENERATING = "regenerating"
    APPROVAL_FINISHED = "approval_finished"
    APPROVAL_CANCELLED = "approval_cancelled"

class ApprovalManager:
    def __init__(self, tool_state_manager: ToolStateManager, db: RinDB, llm_service: LLMService):
        """Initialize approval manager with required services"""
        logger.info("Initializing ApprovalManager...")
        self.tool_state_manager = tool_state_manager
        self.db = db
        self.llm_service = llm_service
        self.analyzer = ApprovalAnalyzer(llm_service)
        logger.info("ApprovalManager initialized successfully")

    # Mapping between Approval States and Tool States
    STATE_MAPPING = {
        ApprovalState.AWAITING_INITIAL: ToolOperationState.APPROVING,
        ApprovalState.AWAITING_APPROVAL: ToolOperationState.APPROVING,
        ApprovalState.PARTIALLY_APPROVED: ToolOperationState.APPROVING,
        ApprovalState.REGENERATING: ToolOperationState.APPROVING,
        ApprovalState.APPROVAL_FINISHED: ToolOperationState.EXECUTING,
        ApprovalState.APPROVAL_CANCELLED: ToolOperationState.CANCELLED
    }

    async def start_approval_flow(self, session_id: str, tool_operation_id: str, items: List[Dict]) -> Dict:
        """Initialize approval flow with proper state and metadata"""
        try:
            logger.info(f"Starting approval flow for {len(items)} items")
            
            # Update items to APPROVING state
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "state": ToolOperationState.COLLECTING.value
                },
                {"$set": {
                    "state": ToolOperationState.APPROVING.value,
                    "status": OperationStatus.PENDING.value,
                    "metadata": {
                        "approval_started_at": datetime.now(UTC).isoformat()
                    }
                }}
            )

            # Update operation state first
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.APPROVING.value,  # Important: Set to APPROVING
                metadata={
                    "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                    "pending_items": [str(item.get('_id')) for item in items],
                    "total_items": len(items)
                }
            )
            
            formatted_items = self.analyzer.format_items_for_review(items)
            
            return {
                "approval_status": "awaiting_approval",
                "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                "response": f"Here are the items for your review:\n\n{formatted_items}",
                "data": {
                    "items": items,  # Move items into data
                    "formatted_items": formatted_items,
                    "pending_count": len(items),
                    "tool_operation_id": tool_operation_id
                }
            }

        except Exception as e:
            logger.error(f"Error starting approval flow: {e}")
            return self.analyzer.create_error_response(str(e))

    async def process_approval_response(
        self,
        message: str,
        session_id: str,
        content_type: str,
        tool_operation_id: str,
        handlers: Dict[str, Callable]
    ) -> Dict:
        """Process user's response during approval flow"""
        try:
            # Get current items for this operation using tool_operation_id
            items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value,
                "status": {"$ne": OperationStatus.REJECTED.value}
            }).to_list(None)

            if not items:
                logger.error(f"No items found for approval in operation {tool_operation_id}")
                return self.analyzer.create_error_response("No items found for approval")

            # Get current operation state
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                logger.error(f"Operation {tool_operation_id} not found")
                return self.analyzer.create_error_response("Operation not found")

            # Analyze the response with the current items
            analysis = await self.analyzer.analyze_response(
                user_response=message,
                current_items=items
            )
            
            # Map the analysis to an action
            action = self._map_to_approval_action(analysis)
            
            if action == ApprovalAction.ERROR:
                # Keep operation in AWAITING_APPROVAL state
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    tool_operation_id=tool_operation_id,
                    state=ToolOperationState.APPROVING.value,
                    metadata={
                        "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                        "last_response_at": datetime.now(UTC).isoformat(),
                        "error": "Could not determine action from response"
                    }
                )
                return self.analyzer.create_error_response("Could not determine action from response")
            
            if action == ApprovalAction.AWAITING_INPUT:
                # Keep operation in AWAITING_APPROVAL state
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    tool_operation_id=tool_operation_id,
                    state=ToolOperationState.APPROVING.value,
                    metadata={
                        "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                        "last_response_at": datetime.now(UTC).isoformat(),
                        "awaiting_input": True
                    }
                )
                return self.analyzer.create_awaiting_response()
            
            # For valid actions (FULL_APPROVAL, PARTIAL_APPROVAL, REGENERATE_ALL)
            # Get the appropriate handler
            handler = handlers.get(action.value)
            if not handler:
                logger.error(f"No handler found for action {action}")
                # Keep operation in AWAITING_APPROVAL state
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    tool_operation_id=tool_operation_id,
                    state=ToolOperationState.APPROVING.value,
                    metadata={
                        "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                        "last_response_at": datetime.now(UTC).isoformat(),
                        "error": f"No handler for action {action}"
                    }
                )
                return self.analyzer.create_error_response(f"No handler for action {action}")
            
            # Call the handler with the analysis
            # Each handler is responsible for:
            # - FULL_APPROVAL: All items -> EXECUTING state
            # - PARTIAL_APPROVAL: Approved -> EXECUTING, Rejected -> new items in COLLECTING
            # - REGENERATE_ALL: All items -> new items in COLLECTING
            return await handler(
                tool_operation_id=tool_operation_id,
                session_id=session_id,
                analysis=analysis
            )

        except Exception as e:
            logger.error(f"Error processing approval response: {e}")
            # Keep operation in AWAITING_APPROVAL state on error
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.APPROVING.value,
                metadata={
                    "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                    "last_response_at": datetime.now(UTC).isoformat(),
                    "error": str(e)
                }
            )
            return self.analyzer.create_error_response(str(e))

    async def handle_full_approval(self, session_id: str, tool_operation_id: str, **kwargs) -> Dict:
        """Handle full approval of all items"""
        try:
            # Update all pending items to EXECUTING state and APPROVED status
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "state": ToolOperationState.APPROVING.value
                },
                {
                    "$set": {
                        "state": ToolOperationState.EXECUTING.value,
                        "status": OperationStatus.APPROVED.value,
                        "last_updated": datetime.now(UTC)
                    }
                }
            )

            # Let ToolStateManager determine if ALL items are approved and update operation accordingly
            await self.tool_state_manager.update_operation_state(tool_operation_id)
            
            # Update approval workflow metadata only
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                    "last_updated": datetime.now(UTC).isoformat()
                }
            )

            return {
                "status": "approved",
                "message": "All items approved"
            }

        except Exception as e:
            logger.error(f"Error in handle_full_approval: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_partial_approval(
        self,
        session_id: str,
        tool_operation_id: str,
        approved_indices: List[int],
        items: List[Dict]
    ) -> Dict:
        """Handle partial approval of items"""
        try:
            logger.info(f"Processing partial approval for operation {tool_operation_id}")
            logger.info(f"Approved indices: {approved_indices}")
            
            # Get all items for this operation
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value
            }).to_list(None)

            if not current_items:
                logger.error("No items found for partial approval")
                return self.analyzer.create_error_response("No items found for approval")

            # Track items for different states
            approved_items = []
            rejected_items = []
            regeneration_needed = []

            for idx, item in enumerate(current_items):
                item_id = str(item['_id'])
                if idx in approved_indices:
                    logger.info(f"Approving item {item_id}")
                    await self.db.tool_items.update_one(
                        {"_id": item['_id']},
                        {"$set": {
                            "state": ToolOperationState.EXECUTING.value,
                            "status": OperationStatus.APPROVED.value,
                            "metadata": {
                                **item.get("metadata", {}),
                                "approved_at": datetime.now(UTC).isoformat()
                            }
                        }}
                    )
                    approved_items.append(item_id)
                else:
                    logger.info(f"Rejecting item {item_id} for regeneration")
                    await self.db.tool_items.update_one(
                        {"_id": item['_id']},
                        {"$set": {
                            "state": ToolOperationState.COMPLETED.value,  # Changed from CANCELLED
                            "status": OperationStatus.REJECTED.value,
                            "metadata": {
                                **item.get("metadata", {}),
                                "rejected_at": datetime.now(UTC).isoformat()
                            }
                        }}
                    )
                    rejected_items.append(item_id)
                    regeneration_needed.append(idx)

            # Only update operation metadata, let tool handle regeneration
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "approval_state": ApprovalState.PARTIALLY_APPROVED.value,
                    "approved_items": approved_items,
                    "rejected_items": rejected_items,
                    "last_updated": datetime.now(UTC).isoformat()
                }
            )

            logger.info(f"Partial approval complete. Approved: {len(approved_items)}, Rejected: {len(rejected_items)}")
            return {
                "status": "partial_approval",
                "approved_count": len(approved_items),
                "regenerate_count": len(rejected_items),
                "regeneration_needed": True,
                "response": f"Approved {len(approved_items)} items. {len(rejected_items)} items need regeneration.",
                "data": {
                    "approved_items": approved_items,
                    "rejected_items": rejected_items,
                    "completion_type": "partial"
                }
            }

        except Exception as e:
            logger.error(f"Error in handle_partial_approval: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_regenerate_all(
        self,
        session_id: str,
        tool_operation_id: str,
        **kwargs
    ) -> Dict:
        """Handle regeneration of all items"""
        try:
            # Get all current items
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value
            }).to_list(None)

            if not current_items:
                logger.error("No items found for regeneration")
                return self.analyzer.create_error_response("No items found")

            logger.info(f"Marking {len(current_items)} items for regeneration")

            # Mark all items as rejected and COMPLETED (not COLLECTING)
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "state": ToolOperationState.APPROVING.value
                },
                {"$set": {
                    "state": ToolOperationState.COMPLETED.value,  # Changed from COLLECTING
                    "status": OperationStatus.REJECTED.value,
                    "metadata": {
                        "rejected_at": datetime.now(UTC).isoformat(),
                        "rejection_reason": "regenerate_all requested"
                    }
                }}
            )

            # Update operation metadata
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "approval_state": ApprovalState.REGENERATING.value,
                    "last_action": "regenerate_all",
                    "items_rejected": len(current_items),
                    "regeneration_requested_at": datetime.now(UTC).isoformat()
                }
            )

            return {
                "status": "regeneration_needed",
                "regenerate_count": len(current_items),
                "response": f"All {len(current_items)} items will be regenerated.",
                "data": {
                    "completion_type": "regenerate_all",
                    "items_to_regenerate": len(current_items)
                }
            }

        except Exception as e:
            logger.error(f"Error in handle_regenerate_all: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_exit(
        self,
        session_id: str,
        tool_operation_id: str,
        success: bool,
        tool_type: str
    ) -> Dict:
        """Handle exit from approval flow"""
        try:
            logger.info(f"Handling exit for operation {tool_operation_id}")
            
            # Get current items
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value
            }).to_list(None)

            if current_items:
                logger.info(f"Found {len(current_items)} pending items to cancel")
                # Cancel any remaining items
                await self.db.tool_items.update_many(
                    {
                        "tool_operation_id": tool_operation_id,
                        "state": ToolOperationState.APPROVING.value
                    },
                    {"$set": {
                        "state": ToolOperationState.CANCELLED.value,
                        "status": OperationStatus.REJECTED.value,
                        "metadata": {
                            "cancelled_at": datetime.now(UTC).isoformat(),
                            "cancel_reason": "Operation exited"
                        }
                    }}
                )
                logger.info(f"Cancelled {len(current_items)} pending items")

            # Update operation state
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.CANCELLED.value,
                metadata={
                    "approval_state": ApprovalState.APPROVAL_CANCELLED.value,
                    "exit_success": success,
                    "exit_time": datetime.now(UTC).isoformat()
                }
            )

            exit_response = self.analyzer.create_exit_response(success, tool_type)
            exit_response["state"] = "cancelled"  # Add missing state key
            return exit_response

        except Exception as e:
            logger.error(f"Error in handle_exit: {e}")
            return self.analyzer.create_error_response(str(e))

    async def _get_tool_exit_details(self, tool_type: str) -> Dict:
        """Get tool-specific exit messaging and status"""
        base_exits = {
            "twitter": {
                "success": {
                    "reason": "Tool operation approved and activated",
                    "status": "APPROVED",
                    "exit_message": "Great! I've scheduled those items for you. What else would you like to do?"
                },
                "cancelled": {
                    "reason": "Tool operation cancelled by user",
                    "status": "CANCELLED", 
                    "exit_message": "I've cancelled the tool operation. What would you like to do instead?"
                }
            },
            # Add other tools here
        }
        
        return base_exits.get(tool_type, {}).get(
            "success" if success else "cancelled",
            self.analyzer.get_default_exit_details(success)
        )

    def _map_to_approval_action(self, analysis: Dict) -> ApprovalAction:
        """Map LLM analysis to ApprovalAction enum"""
        try:
            action = analysis.get("action", "").lower()
            
            # Direct action mapping
            action_map = {
                "full_approval": ApprovalAction.FULL_APPROVAL,
                "partial_approval": ApprovalAction.PARTIAL_APPROVAL,
                "regenerate_all": ApprovalAction.REGENERATE_ALL,
                "exit": ApprovalAction.EXIT,
                "cancel": ApprovalAction.EXIT,
                "stop": ApprovalAction.EXIT,
                "awaiting_input": ApprovalAction.AWAITING_INPUT,
                "error": ApprovalAction.ERROR
            }
            
            # Check for exact matches first
            if action in action_map:
                logger.info(f"Mapped action '{action}' to {action_map[action]}")
                return action_map[action]
            
            # Check for partial matches
            for key, value in action_map.items():
                if key in action:
                    logger.info(f"Mapped partial match '{action}' to {value}")
                    return value
            
            # Handle regeneration
            if any(term in action for term in ["regenerate", "redo", "retry"]):
                logger.info("Mapped to REGENERATE due to regeneration request")
                return ApprovalAction.REGENERATE
            
            logger.warning(f"No mapping found for action: {action}")
            return ApprovalAction.ERROR
            
        except Exception as e:
            logger.error(f"Error in action mapping: {e}")
            return ApprovalAction.ERROR

    def _get_default_exit_details(self, success: bool) -> Dict:
        """Get default exit details based on success"""
        return {
            "reason": "Operation completed successfully" if success else "Operation failed with error",
            "status": OperationStatus.APPROVED.value if success else OperationStatus.FAILED.value,
            "exit_message": "Great! All done. What else would you like to discuss?" if success else "I encountered an error. Let's try something else. What would you like to do?"
        }

    async def _get_approval_state(self, operation: Dict) -> ApprovalState:
        """Get current approval state from operation metadata"""
        approval_state = operation.get('metadata', {}).get('approval_state')
        try:
            return ApprovalState(approval_state)
        except (ValueError, TypeError):
            logger.warning(f"Invalid approval state: {approval_state}, defaulting to AWAITING_INITIAL")
            return ApprovalState.AWAITING_INITIAL