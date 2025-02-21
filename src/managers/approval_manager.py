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
    ToolType,
    ApprovalState
)
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService, ModelType
from pymongo import MongoClient
from src.services.approval_analyzer import ApprovalAnalyzer
from src.managers.schedule_manager import ScheduleManager

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
    def __init__(self, tool_state_manager: ToolStateManager, db: RinDB, llm_service: LLMService, schedule_manager: ScheduleManager):
        """Initialize approval manager with required services"""
        logger.info("Initializing ApprovalManager...")
        self.tool_state_manager = tool_state_manager
        self.db = db
        self.llm_service = llm_service
        self.analyzer = ApprovalAnalyzer(llm_service)
        self.schedule_manager = schedule_manager
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

            # Update operation state
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.APPROVING.value,
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
            # Get current items for this operation
            items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value,
                "status": {"$ne": OperationStatus.REJECTED.value}
            }).to_list(None)

            if not items:
                logger.error(f"No items found for approval in operation {tool_operation_id}")
                return self.analyzer.create_error_response("No items found for approval")

            # Log items being analyzed
            logger.info(f"Analyzing {len(items)} items for approval")
            for item in items:
                logger.info(f"Item {item['_id']}: state={item['state']}, status={item.get('status')}")

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

            # For partial approval, we need to handle both APPROVAL_FINISHED and REGENERATING states
            if action == ApprovalAction.PARTIAL_APPROVAL:
                approved_indices = analysis.get('indices', [])
                regenerate_indices = analysis.get('regenerate_indices', [])
                
                # Important: Set operation state to COLLECTING first if we have items to regenerate
                if regenerate_indices:
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        tool_operation_id=tool_operation_id,
                        state=ToolOperationState.COLLECTING.value,
                        metadata={
                            "approval_state": ApprovalState.REGENERATING.value,
                            "regenerate_count": len(regenerate_indices)
                        }
                    )
                
                # Then update individual items
                if approved_indices:
                    await self._update_approved_items(tool_operation_id, approved_indices, items)
                if regenerate_indices:
                    await self._update_rejected_items(tool_operation_id, regenerate_indices, items)
                
                logger.info(f"Calling partial approval handler with {len(regenerate_indices)} regenerations")
                handler = handlers.get(action.value)
                regen_result = await handler(
                    tool_operation_id=tool_operation_id,
                    session_id=session_id,
                    analysis=analysis,
                    regenerate_count=len(regenerate_indices)
                )

                # Attach the original analysis so subsequent turns can still see it:
                regen_result["analysis"] = analysis
                return regen_result
            
            # For other actions, use standard handler
            handler = handlers.get(action.value)
            if not handler:
                logger.error(f"No handler found for action {action}")
                return self.analyzer.create_error_response(f"No handler for action {action}")
            
            # Call the handler with the analysis and tool_operation_id
            return await handler(
                tool_operation_id=tool_operation_id,
                session_id=session_id,
                analysis=analysis,
                regenerate_count=len(analysis.get('regenerate_indices', [])) # Pass regenerate count explicitly
            )

        except Exception as e:
            logger.error(f"Error processing approval response: {e}")
            return self.analyzer.create_error_response(str(e))

    async def _update_approved_items(self, tool_operation_id: str, approved_indices: List[int], items: List[Dict]):
        """Update approved items to APPROVAL_FINISHED state"""
        approved_ids = [items[idx]['_id'] for idx in approved_indices if 0 <= idx < len(items)]
        logger.info(f"Updating {len(approved_ids)} items to APPROVED/EXECUTING state")
        
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

    async def _update_rejected_items(self, tool_operation_id: str, regenerate_indices: List[int], items: List[Dict]):
        """Update rejected items to COMPLETED state"""
        rejected_ids = [items[idx]['_id'] for idx in regenerate_indices if 0 <= idx < len(items)]
        logger.info(f"Updating {len(rejected_ids)} items to REJECTED/COMPLETED state")
        
        await self.db.tool_items.update_many(
            {
                "tool_operation_id": tool_operation_id,
                "_id": {"$in": rejected_ids}
            },
            {"$set": {
                "state": ToolOperationState.COMPLETED.value,
                "status": OperationStatus.REJECTED.value,
                "metadata.rejected_at": datetime.now(UTC).isoformat()
            }}
        )
        logger.info(f"Successfully updated items {rejected_ids} to REJECTED/COMPLETED")

    async def _handle_full_approval(
        self,
        tool_operation_id: str,
        session_id: str,
        items: List[Dict],
        analysis: Dict
    ) -> Dict:
        """Handle full approval of all items"""
        try:
            logger.info(f"Handling full approval for operation {tool_operation_id}")
            
            # 1. Get current operation to check history
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            # 2. Update all current items to EXECUTING state
            await self._update_approved_items(
                tool_operation_id,
                list(range(len(items))),  # All current items
                items
            )
            
            # 3. Get all items for this operation, including rejected ones
            all_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id
            }).to_list(None)
            
            # Count items by state for metadata
            executing_items = [i for i in all_items if i['state'] == ToolOperationState.EXECUTING.value]
            rejected_items = [i for i in all_items if i['status'] == OperationStatus.REJECTED.value]
            
            logger.info(f"Operation summary - Executing: {len(executing_items)}, "
                       f"Previously Rejected: {len(rejected_items)}")

            # 4. Update operation state with complete item tracking
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.EXECUTING.value,
                metadata={
                    "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                    "item_summary": {
                        "total_items_generated": len(all_items),
                        "final_executing_count": len(executing_items),
                        "total_rejected_count": len(rejected_items),
                        "approved_item_ids": [str(i['_id']) for i in executing_items],
                        "rejected_item_ids": [str(i['_id']) for i in rejected_items],
                        "approval_completed_at": datetime.now(UTC).isoformat()
                    }
                }
            )
            
            # Get operation to check if it's schedulable
            is_schedulable = operation.get('metadata', {}).get('is_schedulable', False)
            schedule_info = operation.get('input_data', {}).get('command_info', {}).get('schedule_info')

            if is_schedulable and schedule_info:
                # Trigger scheduling flow
                schedule_success = await self.schedule_manager.activate_schedule(
                    tool_operation_id=tool_operation_id,
                    schedule_info=schedule_info,
                    content_type=operation['metadata']['content_type']
                )
                if not schedule_success:
                    return self._create_error_response("Failed to activate schedule")

            return {
                "status": OperationStatus.APPROVED.value,
                "state": ToolOperationState.EXECUTING.value,
                "approval_state": ApprovalState.APPROVAL_FINISHED.value,
                "message": "Items approved and scheduled for execution",
                "data": {
                    "executing_items": executing_items,
                    "rejected_items": rejected_items,
                    "scheduled": is_schedulable
                }
            }
            
        except Exception as e:
            logger.error(f"Error in full approval handler: {e}")
            return self._create_error_response(str(e))

    async def handle_partial_approval(
        self,
        session_id: str,
        tool_operation_id: str,
        analysis: Dict
    ) -> Dict:
        """Handle partial approval of items"""
        try:
            logger.info(f"Processing partial approval for operation {tool_operation_id}")
            approved_indices = analysis.get('indices', [])
            regenerate_indices = analysis.get('regenerate_indices', [])
            logger.info(f"Approved indices: {approved_indices}")
            logger.info(f"Regenerate indices: {regenerate_indices}")
            
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

            # Update approved items first
            if approved_indices:
                logger.info(f"Updating {len(approved_indices)} items to EXECUTING state")
                await self._update_approved_items(tool_operation_id, approved_indices, current_items)
                approved_items = [current_items[idx] for idx in approved_indices if 0 <= idx < len(current_items)]

            # Update rejected items - just mark them as COMPLETED/REJECTED
            if regenerate_indices:
                logger.info(f"Updating {len(regenerate_indices)} items to COMPLETED state")
                await self._update_rejected_items(tool_operation_id, regenerate_indices, current_items)
                rejected_items = [current_items[idx] for idx in regenerate_indices if 0 <= idx < len(current_items)]

            # Update operation state for regeneration
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.COLLECTING.value,  # Let _generate_tweets handle this
                metadata={
                    "approval_state": ApprovalState.REGENERATING.value,
                    "regenerate_count": len(regenerate_indices)
                }
            )

            return {
                "status": "regeneration_needed",
                "regenerate_count": len(regenerate_indices),
                "response": f"{len(approved_indices)} items approved, {len(regenerate_indices)} to regenerate."
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