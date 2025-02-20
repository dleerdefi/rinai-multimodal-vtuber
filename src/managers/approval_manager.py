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

    async def start_approval_flow(self, session_id: str, operation_id: str, items: List[Dict]) -> Dict:
        """Initialize approval flow with proper state and metadata"""
        try:
            logger.info(f"Starting approval flow for {len(items)} items")
            
            # First, update all items to PENDING status while keeping state as COLLECTING
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": operation_id,
                    "status": ToolOperationState.COLLECTING.value
                },
                {"$set": {
                    "status": ToolOperationState.APPROVING.value,
                    "metadata.approval_started_at": datetime.now(UTC).isoformat()
                }}
            )

            # Update operation metadata while keeping state as COLLECTING
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                operation_id=operation_id,
                state=ToolOperationState.APPROVING.value, 
                step="awaiting_approval",
                metadata={
                    "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                    "awaiting_approval": True,
                    "pending_items": [str(item.get('_id')) for item in items],
                    "approved_items": [],
                    "rejected_items": [],
                    "initial_generation": True,
                    "total_items": len(items),
                    "approval_started_at": datetime.now(UTC).isoformat()
                }
            )
            
            # Format items for review
            formatted_response = self.analyzer.format_items_for_review(items)
            logger.info("Generated formatted response for review")
            
            return {
                "status": "awaiting_approval",
                "response": formatted_response,
                "requires_tts": True,
                "data": {
                    "items": items,
                    "operation_id": operation_id,
                    "pending_count": len(items),
                    "approved_count": 0,
                    "rejected_count": 0
                }
            }

        except Exception as e:
            logger.error(f"Error in start_approval_flow: {e}")
            return self.analyzer.create_error_response(str(e))

    async def process_approval_response(
        self,
        message: str,
        session_id: str,
        content_type: str,
        content_id: str,
        handlers: Dict[str, Callable]
    ) -> Dict:
        """Process user's response during approval flow"""
        try:
            # Get current items for this operation using content_id
            items = await self.db.tool_items.find({
                "tool_operation_id": content_id,
                "status": ToolOperationState.APPROVING.value,
                "operation_status": {"$ne": OperationStatus.REJECTED.value}
            }).to_list(None)

            if not items:
                logger.error(f"No items found for approval in operation {content_id}")
                return self.analyzer.create_error_response("No items found for approval")

            # Get current operation state
            operation = await self.tool_state_manager.get_operation_by_id(content_id)
            if not operation:
                logger.error(f"Operation {content_id} not found")
                return self.analyzer.create_error_response("Operation not found")

            # Analyze the response with the current items
            analysis = await self.analyzer.analyze_response(
                user_response=message,
                current_items=items
            )
            
            # Map the analysis to an action
            action = self._map_to_approval_action(analysis)
            
            if action == ApprovalAction.ERROR:
                return self.analyzer.create_error_response("Could not determine action from response")
            
            if action == ApprovalAction.AWAITING_INPUT:
                return self.analyzer.create_awaiting_response()
            
            # Get the appropriate handler
            handler = handlers.get(action.value)
            if not handler:
                logger.error(f"No handler found for action {action}")
                return self.analyzer.create_error_response(f"No handler for action {action}")
            
            # Call the handler with the analysis
            return await handler(
                content_id=content_id,
                session_id=session_id,
                analysis=analysis
            )

        except Exception as e:
            logger.error(f"Error processing approval response: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_full_approval(
        self,
        session_id: str,
        operation_id: str,
        **kwargs
    ) -> Dict:
        """Handle full approval of all items"""
        try:
            logger.info("Handling full approval")
            
            # First get all items in APPROVING state that haven't been rejected
            items = await self.db.tool_items.find({
                "tool_operation_id": operation_id,
                "status": ToolOperationState.APPROVING.value,
                "operation_status": {"$ne": OperationStatus.REJECTED.value}  # Exclude previously rejected items
            }).to_list(None)
            
            if not items:
                logger.warning("No items found in APPROVING state")
                return {
                    "status": "error",
                    "message": "No items found for approval"
                }
            
            # Update only non-rejected items to EXECUTING state
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": operation_id,
                    "status": ToolOperationState.APPROVING.value,
                    "operation_status": {"$ne": OperationStatus.REJECTED.value}  # Exclude previously rejected items
                },
                {"$set": {
                    "status": ToolOperationState.EXECUTING.value,
                    "operation_status": OperationStatus.APPROVED.value,
                    "metadata.approved_at": datetime.now(UTC).isoformat()
                }}
            )
            
            # Update operation state to EXECUTING
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                operation_id=operation_id,
                state=ToolOperationState.EXECUTING.value,
                step="executing",
                metadata={
                    "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                    "approved_items": [str(item.get('_id')) for item in items],
                    "rejected_items": [],
                    "approval_completed_at": datetime.now(UTC).isoformat()
                }
            )
            
            return {
                "status": "approved",
                "message": "All items approved",
                "approved_count": len(items)
            }
            
        except Exception as e:
            logger.error(f"Error in full approval: {e}")
            raise

    async def handle_partial_approval(
        self,
        session_id: str,
        operation_id: str,
        approved_indices: List[int],
        items: List[Dict],
        **kwargs
    ) -> Dict:
        """Handle partial approval of items"""
        try:
            logger.info(f"Handling partial approval for {len(approved_indices)} items")
            
            # Update approved items to EXECUTING state and APPROVED status
            if approved_indices:
                approved_item_ids = [str(items[idx].get('_id')) for idx in approved_indices]
                await self.db.tool_items.update_many(
                    {
                        "_id": {"$in": [ObjectId(id) for id in approved_item_ids]},
                        "tool_operation_id": operation_id
                    },
                    {"$set": {
                        "status": ToolOperationState.EXECUTING.value,
                        "operation_status": OperationStatus.APPROVED.value,
                        "metadata.approved_at": datetime.now(UTC).isoformat()
                    }}
                )
                logger.info(f"Updated {len(approved_item_ids)} items to EXECUTING state and APPROVED status")
            
            # Update rejected items back to COLLECTING state and PENDING status
            rejected_indices = [i for i in range(len(items)) if i not in approved_indices]
            if rejected_indices:
                rejected_item_ids = [str(items[idx].get('_id')) for idx in rejected_indices]
                await self.db.tool_items.update_many(
                    {
                        "_id": {"$in": [ObjectId(id) for id in rejected_item_ids]},
                        "tool_operation_id": operation_id
                    },
                    {"$set": {
                        "status": ToolOperationState.COLLECTING.value,
                        "operation_status": OperationStatus.PENDING.value,
                        "metadata.rejected_at": datetime.now(UTC).isoformat(),
                        "metadata.regeneration_pending": True
                    }}
                )
                logger.info(f"Updated {len(rejected_item_ids)} items to COLLECTING state and PENDING status")

                # If there are rejected items, transition operation to COLLECTING
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    operation_id=operation_id,
                    state=ToolOperationState.COLLECTING.value,
                    step="regenerating",
                    metadata={
                        "approval_state": ApprovalState.REGENERATING.value,
                        "approved_items": [str(items[idx].get('_id')) for idx in approved_indices],
                        "rejected_items": [str(items[idx].get('_id')) for idx in rejected_indices],
                        "regeneration_pending": True,
                        "partial_approval_at": datetime.now(UTC).isoformat()
                    }
                )
            else:
                # If all items approved, transition to EXECUTING
                await self.tool_state_manager.update_operation(
                    session_id=session_id,
                    operation_id=operation_id,
                    state=ToolOperationState.EXECUTING.value,
                    step="executing",
                    metadata={
                        "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                        "approved_items": [str(items[idx].get('_id')) for idx in approved_indices],
                        "rejected_items": [],
                        "approval_completed_at": datetime.now(UTC).isoformat()
                    }
                )
            
            return {
                "status": "partial_approval",
                "approved_count": len(approved_indices),
                "rejected_count": len(rejected_indices),
                "regeneration_needed": len(rejected_indices) > 0
            }
            
        except Exception as e:
            logger.error(f"Error in partial approval: {e}")
            raise

    async def handle_regenerate_all(
        self,
        session_id: str,
        operation_id: str,
        **kwargs
    ) -> Dict:
        """Handle regeneration request for all items"""
        try:
            logger.info("Handling regenerate all request")
            
            # Get all items in APPROVING state
            items = await self.db.tool_items.find({
                "tool_operation_id": operation_id,
                "status": ToolOperationState.APPROVING.value
            }).to_list(None)
            
            if not items:
                logger.warning("No items found in APPROVING state")
                return {
                    "status": "error",
                    "message": "No items found for regeneration"
                }
            
            # Update all items to COLLECTING state and REJECTED status
            item_ids = [str(item.get('_id')) for item in items]
            await self.db.tool_items.update_many(
                {
                    "_id": {"$in": [ObjectId(id) for id in item_ids]},
                    "tool_operation_id": operation_id
                },
                {"$set": {
                    "status": ToolOperationState.COLLECTING.value,
                    "operation_status": OperationStatus.REJECTED.value,
                    "metadata.rejected_at": datetime.now(UTC).isoformat(),
                    "metadata.regeneration_pending": True
                }}
            )
            logger.info(f"Updated {len(item_ids)} items to COLLECTING state and REJECTED status")

            # Update operation to COLLECTING state
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                operation_id=operation_id,
                state=ToolOperationState.COLLECTING.value,
                step="regenerating",
                metadata={
                    "approval_state": ApprovalState.REGENERATING.value,
                    "rejected_items": item_ids,
                    "regeneration_pending": True,
                    "regeneration_requested_at": datetime.now(UTC).isoformat()
                }
            )
            
            return {
                "status": "regenerating",
                "regenerate_count": len(items),
                "message": f"Regenerating {len(items)} items"
            }
            
        except Exception as e:
            logger.error(f"Error in regeneration request: {e}")
            raise

    async def handle_exit(
        self,
        session_id: str,
        operation_id: str,  # Add operation_id parameter
        success: bool = False,
        tool_type: str = None
    ) -> Dict:
        """Handle exit from approval workflow"""
        try:
            logger.info(f"Handling exit for operation {operation_id}")
            
            # Remove the operation lookup since we now have operation_id
            # operation = await self.tool_state_manager.get_operation(session_id)
            # operation_id = str(operation['_id'])

            # Get all items for this operation
            items = await self.db.tool_items.find({
                "tool_operation_id": operation_id
            }).to_list(None)

            if not success:
                # Find items still in approval workflow
                pending_items = [item for item in items if (
                    item.get("status") in [
                        ToolOperationState.COLLECTING.value,
                        ToolOperationState.APPROVING.value
                    ] and
                    item.get("operation_status") == OperationStatus.PENDING.value
                )]

                if pending_items:
                    # Cancel pending items
                    pending_item_ids = [str(item.get('_id')) for item in pending_items]
                    await self.db.tool_items.update_many(
                        {
                            "_id": {"$in": [ObjectId(id) for id in pending_item_ids]},
                            "tool_operation_id": operation_id
                        },
                        {"$set": {
                            "status": ToolOperationState.CANCELLED.value,
                            "operation_status": OperationStatus.REJECTED.value,
                            "metadata.cancelled_at": datetime.now(UTC).isoformat(),
                            "metadata.approval_state": ApprovalState.APPROVAL_CANCELLED.value
                        }}
                    )
                    
                    logger.info(f"Cancelled {len(pending_items)} pending items")

                    # Update operation approval state before ending
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        operation_id=operation_id,
                        metadata={
                            "approval_state": ApprovalState.APPROVAL_CANCELLED.value,
                            "cancelled_at": datetime.now(UTC).isoformat()
                        }
                    )

                    # End operation (this handles the state transition)
                    await self.tool_state_manager.end_operation(
                        session_id=session_id,
                        operation_id=operation_id,
                        status=OperationStatus.REJECTED,
                        reason="User cancelled operation"
                    )
                    
                    return self.analyzer.create_exit_response(success, tool_type)

        except Exception as e:
            logger.error(f"Error handling exit: {e}")
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