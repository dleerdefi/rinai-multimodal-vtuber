from typing import Dict, List, Callable, Optional, Any
from datetime import datetime, UTC
import logging
from enum import Enum
from bson.objectid import ObjectId
import json
from src.db.db_schema import (
    RinDB, 
    ToolOperation
)
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService, ModelType
from pymongo import MongoClient
from src.services.approval_analyzer import ApprovalAnalyzer
from src.managers.schedule_manager import ScheduleManager
from src.db.enums import OperationStatus, ToolOperationState, ApprovalState
logger = logging.getLogger(__name__)

class ApprovalAction(Enum):
    """User actions that trigger state transitions"""
    FULL_APPROVAL = "full_approval"
    PARTIAL_APPROVAL = "partial_approval"
    REGENERATE_ALL = "regenerate_all"
    AWAITING_INPUT = "awaiting_input"
    ERROR = "error"
    EXIT = "exit"

class ApprovalManager:
    def __init__(self, tool_state_manager: ToolStateManager, db: RinDB, llm_service: LLMService, schedule_manager: ScheduleManager, orchestrator=None):
        """Initialize approval manager with required services"""
        logger.info("Initializing ApprovalManager...")
        self.tool_state_manager = tool_state_manager
        self.db = db
        self.llm_service = llm_service
        self.analyzer = ApprovalAnalyzer(llm_service)
        self.schedule_manager = schedule_manager
        self.orchestrator = orchestrator  # Store reference to orchestrator
        logger.info("ApprovalManager initialized successfully")

    # Mapping between Approval States and Tool States
    STATE_MAPPING = {
        ApprovalState.AWAITING_INITIAL: ToolOperationState.APPROVING,
        ApprovalState.AWAITING_APPROVAL: ToolOperationState.APPROVING,
        ApprovalState.REGENERATING: ToolOperationState.COLLECTING,      # For rejected items
        ApprovalState.APPROVAL_FINISHED: ToolOperationState.EXECUTING,  # For approved items
        ApprovalState.APPROVAL_CANCELLED: ToolOperationState.CANCELLED
    }

    async def start_approval_flow(
        self,
        session_id: str,
        tool_operation_id: str,
        items: List[Dict],
        analysis: Dict = None,
        **kwargs
    ) -> Dict:
        """Start approval flow for generated items"""
        try:
            logger.info(f"Starting approval flow for {len(items)} items")
            
            # Ensure we have valid items
            if not items:
                raise ValueError("No items provided for approval flow")
            
            # Ensure each item is a dictionary with required fields
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"Invalid item format: {item}")
                if '_id' not in item:
                    raise ValueError(f"Item missing _id field: {item}")
            
            # Update items to APPROVING state
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "state": ToolOperationState.COLLECTING.value
                },
                {"$set": {
                    "state": ToolOperationState.APPROVING.value,
                    "metadata": {
                        "approval_started_at": datetime.now(UTC).isoformat()
                    }
                }}
            )

            # Update operation state
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.APPROVING.value,
                metadata={
                    "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                    "pending_items": [str(item['_id']) for item in items],
                    "total_items": len(items)
                }
            )
            
            formatted_items = self.analyzer.format_items_for_review(items)
            
            return {
                "approval_status": "awaiting_approval",
                "approval_state": ApprovalState.AWAITING_APPROVAL.value,
                "response": f"Here are the items for your review:\n\n{formatted_items}",
                "data": {
                    "items": items,
                    "formatted_items": formatted_items,
                    "pending_count": len(items),
                    "tool_operation_id": tool_operation_id,
                    "analysis": analysis
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
            # Get operation to check active items
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            active_items = operation.get('metadata', {}).get('active_items', [])
            
            # Get ONLY the current turn's pending items using active_items list
            items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value,
                "status": OperationStatus.PENDING.value,
                "metadata.rejected_at": {"$exists": False},
                "_id": {"$in": [ObjectId(id) for id in active_items]} if active_items else {"$exists": True}
            }).to_list(None)

            if not items:
                logger.error(f"No pending items found for approval in operation {tool_operation_id}")
                return self.analyzer.create_error_response("No items found for approval")

            # Log items being analyzed
            logger.info(f"Analyzing {len(items)} pending items")
            for item in items:
                logger.info(f"Item {item['_id']}: state={item['state']}, status={item.get('status')}")

            # Analyze the response with ONLY the current items
            analysis = await self.analyzer.analyze_response(
                user_response=message,
                current_items=items  # Pass only current turn's items
            )
            
            # Map the analysis to an action
            action = self._map_to_approval_action(analysis)
            
            if action == ApprovalAction.ERROR:
                return self.analyzer.create_error_response("Could not determine action from response")
            
            if action == ApprovalAction.AWAITING_INPUT:
                return self.analyzer.create_awaiting_response()

            # For partial approval, use our internal handler
            if action == ApprovalAction.PARTIAL_APPROVAL:
                return await self.handle_partial_approval(
                        session_id=session_id,
                        tool_operation_id=tool_operation_id,
                    analysis=analysis
                )
            
            # For other actions, use the provided handler
            handler = handlers.get(action.value)
            if not handler:
                logger.error(f"No handler found for action {action}")
                return self.analyzer.create_error_response(f"No handler for action {action}")
            
            # Call the handler with the analysis and tool_operation_id
            return await handler(
                tool_operation_id=tool_operation_id,
                session_id=session_id,
                items=items,
                analysis=analysis
            )

        except Exception as e:
            logger.error(f"Error processing approval response: {e}")
            return self.analyzer.create_error_response(str(e))

    async def _update_approved_items(self, tool_operation_id: str, approved_indices: List[int], items: List[Dict]):
        """Update approved items to APPROVAL_FINISHED state"""
        try:
            # Convert 1-based indices to 0-based if needed
            adjusted_indices = [(idx - 1) if idx > 0 else idx for idx in approved_indices]
            
            # Log the conversion for debugging
            logger.info(f"Converting indices {approved_indices} to array indices {adjusted_indices}")
            
            # Validate indices are in range
            valid_indices = [idx for idx in adjusted_indices if 0 <= idx < len(items)]
            if len(valid_indices) != len(adjusted_indices):
                logger.warning(f"Some indices were out of range: {approved_indices}, valid: {valid_indices}")
            
            approved_ids = [items[idx]['_id'] for idx in valid_indices]
            
            logger.info(f"Updating {len(approved_ids)} items to APPROVED/EXECUTING state")
            
            if not approved_ids:
                logger.warning("No valid item IDs to approve")
                return
            
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "_id": {"$in": approved_ids}
                },
                {"$set": {
                    "state": ToolOperationState.EXECUTING.value,
                    "status": OperationStatus.APPROVED.value,
                    "metadata.approval_state": ApprovalState.APPROVAL_FINISHED.value,
                    "metadata.approved_at": datetime.now(UTC).isoformat()
                }}
            )
            logger.info(f"Successfully updated items {approved_ids} to APPROVED/EXECUTING")
        except Exception as e:
            logger.error(f"Error updating approved items: {e}")

    async def _update_rejected_items(self, tool_operation_id: str, regenerate_indices: List[int], items: List[Dict]):
        """Update rejected items to CANCELLED state"""
        # Convert 1-based indices to 0-based if needed
        adjusted_indices = [(idx - 1) if idx > 0 else idx for idx in regenerate_indices]
        rejected_ids = [items[idx]['_id'] for idx in adjusted_indices if 0 <= idx < len(items)]
        
        logger.info(f"Updating {len(rejected_ids)} items to REJECTED/CANCELLED state")
        
        await self.db.tool_items.update_many(
            {
                "tool_operation_id": tool_operation_id,
                "_id": {"$in": rejected_ids}
            },
            {"$set": {
                "state": ToolOperationState.CANCELLED.value,
                "status": OperationStatus.REJECTED.value,
                "metadata.rejected_at": datetime.now(UTC).isoformat()
            }}
        )
        logger.info(f"Successfully updated items {rejected_ids} to REJECTED/CANCELLED")

    async def _handle_full_approval(
        self,
        tool_operation_id: str,
        session_id: str,
        items: List[Dict],
        analysis: Dict
    ) -> Dict:
        """Handle full approval of current turn's items"""
        try:
            logger.info(f"Handling full approval for operation {tool_operation_id}")
            
            # 1. Get operation to check requirements
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")
            
            required_count = operation.get('input_data', {}).get('command_info', {}).get('item_count', 0)
            logger.info(f"Operation requires {required_count} total approved items")

            # 2. Get valid item indices (1-based) for all current items
            valid_item_indices = list(range(1, len(items) + 1))
            
            # 3. Use _update_approved_items for consistent handling
            await self._update_approved_items(
                tool_operation_id=tool_operation_id,
                approved_indices=valid_item_indices,
                items=items
            )

            # 4. Get total approved items count (including previous turns)
            all_approved_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.EXECUTING.value,
                status=OperationStatus.APPROVED.value,
                additional_query={
                    "metadata.rejected_at": {"$exists": False},
                    "metadata.cancelled_at": {"$exists": False}
                }
            )
            
            total_approved = len(all_approved_items)
            logger.info(f"Found {total_approved} total approved items out of {required_count} required")

            # Update operation state to EXECUTING before returning
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.EXECUTING.value,
                status=OperationStatus.APPROVED.value,
                metadata={
                    "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                    "approved_at": datetime.now(UTC).isoformat(),
                    "total_approved": total_approved,
                    "required_count": required_count
                }
            )

            return {
                "status": OperationStatus.APPROVED.value,
                "state": ToolOperationState.EXECUTING.value,
                "message": f"Approved {len(items)} items this turn. Total approved: {total_approved}/{required_count}",
                "data": {
                    "total_approved": total_approved,
                    "required_count": required_count,
                    "current_turn_approved": len(items),
                    "approved_item_ids": [str(item['_id']) for item in all_approved_items]
                }
            }

        except Exception as e:
            logger.error(f"Error in full approval handler: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_partial_approval(
        self,
        session_id: str,
        tool_operation_id: str,
        analysis: Dict
    ) -> Dict:
        try:
            logger.info(f"Processing partial approval for operation {tool_operation_id}")
            
            # Get operation to access schedule_id and other metadata
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            # Process approved items
            approved_indices = analysis.get('approved_indices', [])
            regenerate_indices = analysis.get('regenerate_indices', [])
            
            # Get current items
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value
            }).to_list(None)

            # Handle approved items - these will be linked to existing schedule
            if approved_indices:
                await self._update_approved_items(tool_operation_id, approved_indices, current_items)
                logger.info(f"Updated {len(approved_indices)} items to APPROVED/EXECUTING")

            # Handle rejected items
            if regenerate_indices:
                await self._update_rejected_items(tool_operation_id, regenerate_indices, current_items)
                logger.info(f"Updated {len(regenerate_indices)} items to REJECTED/CANCELLED")

            # Calculate regeneration count
            regenerate_count = len(regenerate_indices)
            if regenerate_count <= 0:
                logger.warning("No items to regenerate")
                return {
                    "status": "completed",
                    "message": "All items processed"
                }

            # Create new items in COLLECTING state
            # Important: Use same tool_operation_id and schedule_id
            new_items = await self.tool_state_manager.create_regeneration_items(
                session_id=session_id,
                tool_operation_id=tool_operation_id,  # Same parent operation
                items_data=[{} for _ in range(regenerate_count)],
                content_type=operation.get('metadata', {}).get('content_type'),
                schedule_id=operation.get('metadata', {}).get('schedule_id'),  # Keep existing schedule
                metadata={
                    "regeneration_reason": "partial_approval",
                    "regenerated_at": datetime.now(UTC).isoformat(),
                    "revision_instructions": analysis.get("revision_instructions"),
                    "parent_operation_id": tool_operation_id,
                    "parent_schedule_id": operation.get('metadata', {}).get('schedule_id')
                }
            )

            # Update operation to COLLECTING state for regeneration
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.COLLECTING.value,
                status=OperationStatus.PENDING.value,
                metadata={
                    "regeneration_needed": True,
                    "approval_state": ApprovalState.REGENERATING.value,
                    "revision_instructions": analysis.get("revision_instructions"),
                    "regenerated_item_ids": [str(item['_id']) for item in new_items],
                    "active_items": [str(item['_id']) for item in new_items],
                    # Keep existing schedule_id
                    "schedule_id": operation.get('metadata', {}).get('schedule_id')
                }
            )

            return {
                "status": "regeneration_needed",
                "data": {
                    "regenerate_count": len(new_items),
                    "analysis": analysis,
                    "regenerated_item_ids": [str(item['_id']) for item in new_items]
                }
            }

        except Exception as e:
            logger.error(f"Error in handle_partial_approval: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_regenerate_all(
        self,
        session_id: str,
        tool_operation_id: str,
        analysis: Dict,
        **kwargs
    ) -> Dict:
        """Handle regeneration of all items"""
        try:
            # 1. First get and properly mark all current PENDING items as REJECTED
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value,
                "status": OperationStatus.PENDING.value,
                "metadata.rejected_at": {"$exists": False}
            }).to_list(None)

            if not current_items:
                logger.error("No pending items found for regeneration")
                return self.analyzer.create_error_response("No items found")

            logger.info(f"Marking {len(current_items)} items for regeneration")

            # 2. Mark current items as REJECTED and store rejection info
            await self.db.tool_items.update_many(
                {
                    "_id": {"$in": [item['_id'] for item in current_items]},
                    "state": ToolOperationState.APPROVING.value,
                    "status": OperationStatus.PENDING.value
                },
                {"$set": {
                    "state": ToolOperationState.CANCELLED.value,
                    "status": OperationStatus.REJECTED.value,
                    "metadata.rejected_at": datetime.now(UTC).isoformat(),
                    "metadata.rejection_reason": "regenerate_all requested",
                    "metadata.revision_instructions": analysis.get("revision_instructions")
                }}
            )
            logger.info(f"Updated {len(current_items)} items to REJECTED/CANCELLED state")

            # 3. Get operation to check required count
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            required_count = operation.get('input_data', {}).get('command_info', {}).get('item_count', len(current_items))

            # 4. Create new items in COLLECTING state
            new_items = await self.tool_state_manager.create_regeneration_items(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                items_data=[{} for _ in range(required_count)],
                content_type=current_items[0]['content_type'],
                schedule_id=operation.get('metadata', {}).get('schedule_id')
            )
            logger.info(f"Created {len(new_items)} new items in COLLECTING state")

            # 5. Update operation state to reflect regeneration
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.COLLECTING.value,
                metadata={
                    "approval_state": ApprovalState.REGENERATING.value,
                    "last_action": "regenerate_all",
                    "items_rejected": len(current_items),
                    "items_to_regenerate": required_count,
                    "regeneration_requested_at": datetime.now(UTC).isoformat(),
                    "revision_instructions": analysis.get("revision_instructions"),
                    "content_type": current_items[0]['content_type'],
                    "active_items": [str(item['_id']) for item in new_items]  # Track active items
                }
            )

            return {
                "status": "regeneration_needed",
                "regenerate_count": required_count,
                "response": f"All {required_count} items will be regenerated.",
                "data": {
                    "completion_type": "regenerate_all",
                    "analysis": analysis,
                    "revision_instructions": analysis.get("revision_instructions"),
                    "active_item_ids": [str(item['_id']) for item in new_items]
                }
            }

        except Exception as e:
            logger.error(f"Error in handle_regenerate_all: {e}")
            return self.analyzer.create_error_response(str(e))

    async def handle_exit(
        self,
        session_id: str,
        tool_operation_id: str,
        success: bool = False,
        tool_type: str = None
    ) -> Dict:
        """Handle exit from approval flow"""
        try:
            logger.info(f"Handling exit for operation {tool_operation_id}")
            
            # Get current items
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": {"$in": [ToolOperationState.APPROVING.value, ToolOperationState.COLLECTING.value]}
            }).to_list(None)

            if current_items:
                logger.info(f"Found {len(current_items)} pending items to cancel")
                # Cancel any remaining items
                await self.db.tool_items.update_many(
                    {
                        "tool_operation_id": tool_operation_id,
                        "state": {"$in": [ToolOperationState.APPROVING.value, ToolOperationState.COLLECTING.value]}
                    },
                    {"$set": {
                        "state": ToolOperationState.CANCELLED.value,
                        "status": OperationStatus.REJECTED.value,
                        "metadata": {
                            "cancelled_at": datetime.now(UTC).isoformat(),
                            "cancel_reason": "User requested cancellation"
                        }
                    }}
                )
                logger.info(f"Cancelled {len(current_items)} pending items")

            # Update the operation state
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.CANCELLED.value,
                step="cancelled",
                metadata={
                    "cancelled_at": datetime.now(UTC).isoformat(),
                    "cancel_reason": "User requested cancellation",
                    "approval_state": ApprovalState.APPROVAL_CANCELLED.value
                }
            )
            
            # End the operation properly
            await self.tool_state_manager.end_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                success=False,
                api_response={
                    "status": "cancelled",
                    "message": "Operation cancelled by user"
                }
            )
            
            # Return a response that includes status="cancelled" to trigger state transition
            return {
                "response": "Operation cancelled. What would you like to do instead?",
                "status": "cancelled",
                "state": ToolOperationState.CANCELLED.value,
                "tool_type": tool_type,
                "requires_tts": True
            }

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

    async def handle_error(
        self,
        session_id: str,
        tool_operation_id: str,
        error_message: str
    ) -> Dict:
        """Handle error during approval flow"""
        try:
            logger.error(f"Handling error in approval flow: {error_message}")
            
            # Update operation state to ERROR through tool_state_manager
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "error": error_message,
                    "error_timestamp": datetime.now(UTC).isoformat(),
                    "approval_state": ApprovalState.ERROR.value
                }
            )
            
            # Call handle_exit to properly clean up and transition state
            return await self.handle_exit(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                success=False,
                tool_type=self._current_tool_type
            )

        except Exception as e:
            logger.error(f"Error handling approval error: {e}")
            return self.analyzer.create_error_response(str(e))