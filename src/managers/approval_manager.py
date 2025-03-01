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
        """Handle full approval of all items"""
        try:
            logger.info(f"Handling full approval for operation {tool_operation_id}")
            
            # 1. Get current operation to check history
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            # 2. Update all current items to APPROVED state
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
            approved_items = [i for i in all_items if i['status'] == OperationStatus.APPROVED.value]
            rejected_items = [i for i in all_items if i['status'] == OperationStatus.REJECTED.value]
            
            logger.info(f"Operation summary - Approved: {len(approved_items)}, "
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
                        "final_approved_count": len(approved_items),
                        "total_rejected_count": len(rejected_items),
                        "approved_item_ids": [str(i['_id']) for i in approved_items],
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
                    "approved_items": approved_items,
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
            
            # Extract indices from analysis
            approved_indices = analysis.get('approved_indices', [])
            regenerate_indices = analysis.get('regenerate_indices', [])
            
            # Get current items
            current_items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id,
                "state": ToolOperationState.APPROVING.value
            }).to_list(None)
            
            if not current_items:
                return self.analyzer.create_error_response("No items found for approval")
            
            # Process approved items
            approved_items = []
            if approved_indices:
                await self._update_approved_items(tool_operation_id, approved_indices, current_items)
                for idx in approved_indices:
                    array_idx = idx - 1 if idx > 0 else idx
                    if 0 <= array_idx < len(current_items):
                        approved_items.append(current_items[array_idx])
            
            # Process rejected items
            rejected_items = []
            if regenerate_indices:
                await self._update_rejected_items(tool_operation_id, regenerate_indices, current_items)
                for idx in regenerate_indices:
                    array_idx = idx - 1 if idx > 0 else idx
                    if 0 <= array_idx < len(current_items):
                        rejected_items.append(current_items[array_idx])
            
            # Get operation details
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")
            
            # Get content type
            content_type = operation.get('metadata', {}).get('content_type', 'tweet')
            
            # Get topic
            topic = "the requested subject"
            if 'input_data' in operation and 'command_info' in operation['input_data']:
                topic = operation['input_data']['command_info'].get('topic', topic)
            elif 'input_data' in operation and 'command' in operation['input_data']:
                command = operation['input_data']['command']
                if 'about' in command:
                    topic = command.split('about')[-1].strip()
            
            # Create better fallback items using LLM directly
            if len(regenerate_indices) > 0:
                # Generate better content using LLM directly
                prompt = f"""You are a professional social media manager. Generate {len(regenerate_indices)} engaging tweets about {topic}.

Guidelines:
- Each tweet should be unique and engaging
- Include relevant hashtags
- Keep within Twitter's character limit
- Vary the style and tone
- Make them informative yet conversational
- Include emojis where appropriate

Format the response as JSON:
{{
    "items": [
        {{
            "content": "Tweet text here",
            "metadata": {{
                "estimated_engagement": "high/medium/low"
            }}
        }}
    ]
}}"""

                messages = [
                    {
                        "role": "system",
                        "content": "You are a professional social media manager. Generate engaging tweets in JSON format."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]

                logger.info(f"Using LLM directly to generate fallback content about {topic}")
                response = await self.llm_service.get_response(
                    prompt=messages,
                    override_config={
                        "temperature": 0.7,
                        "max_tokens": 1000
                    }
                )
                
                # Strip markdown code blocks if present
                response = response.strip()
                if response.startswith('```') and response.endswith('```'):
                    # Remove the first line (```json) and the last line (```)
                    response = '\n'.join(response.split('\n')[1:-1])
                
                try:
                    generated_items = json.loads(response)
                    new_items_data = []
                    
                    for item in generated_items.get('items', []):
                        new_items_data.append({
                            "content": item["content"],
                            "metadata": {
                                **item.get("metadata", {}),
                                "generated_at": datetime.now(UTC).isoformat(),
                                "regenerated": True,
                                "fallback": True,
                                "llm_generated": True
                            }
                        })
                    
                    logger.info(f"Generated {len(new_items_data)} items using LLM directly")
                    
                except json.JSONDecodeError:
                    # If JSON parsing fails, create simple fallback items
                    logger.warning("Failed to parse LLM response, using simple fallback items")
                    new_items_data = []
                    for i in range(len(regenerate_indices)):
                        new_items_data.append({
                            "content": f"Regenerated content about {topic} (fallback item {i+1})",
                            "metadata": {
                                "generated_at": datetime.now(UTC).isoformat(),
                                "regenerated": True,
                                "fallback": True
                            }
                        })
            
            # Create new items
            new_items = await self.tool_state_manager.create_regeneration_items(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                items_data=new_items_data,
                content_type=content_type,
                schedule_id=operation.get('metadata', {}).get('schedule_id')
            )
            
            # Update operation metadata
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "regeneration_completed": True,
                    "regenerated_at": datetime.now(UTC).isoformat(),
                    "regenerated_count": len(new_items),
                    "approval_state": ApprovalState.REGENERATING.value,
                    "content_type": content_type,
                    "used_fallback": True
                }
            )
            
            # Move new items to APPROVING state
            for item in new_items:
                await self.db.tool_items.update_one(
                    {"_id": ObjectId(item["_id"])},
                    {"$set": {
                        "state": ToolOperationState.APPROVING.value,
                        "metadata.approval_started_at": datetime.now(UTC).isoformat()
                    }}
                )
            
            # Format items for review
            all_items = await self.tool_state_manager.get_operation_items(
                tool_operation_id=tool_operation_id,
                state=ToolOperationState.APPROVING.value
            )
            
            formatted_items = self.analyzer.format_items_for_review(all_items)
            
            return {
                "status": "regeneration_completed",
                "response": f"{len(approved_indices)} items approved, {len(regenerate_indices)} regenerated using fallback generation. Please review the new items:\n\n{formatted_items}",
                "requires_tts": True,
                "data": {
                    "approved_items": approved_items,
                    "regenerated_items": new_items,
                    "formatted_items": formatted_items,
                    "used_fallback": True
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

            # Mark all items as rejected and cancelled (not COLLECTING)
            await self.db.tool_items.update_many(
                {
                    "tool_operation_id": tool_operation_id,
                    "state": ToolOperationState.APPROVING.value
                },
                {"$set": {
                    "state": ToolOperationState.CANCELLED.value,  # Changed from COLLECTING
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

            # Update the operation state
            await self.tool_state_manager.update_operation_state(
                tool_operation_id, 
                ToolOperationState.COMPLETED if success else ToolOperationState.CANCELLED
            )
            
            # Get exit details
            exit_details = await self._get_tool_exit_details(tool_type)
            
            # Return a response that includes status="exit" to trigger state transition
            return {
                "response": exit_details.get("exit_message", "Operation complete."),
                "status": "completed" if success else "cancelled",  # Use completed/cancelled for proper state transition
                "state": "completed" if success else "cancelled",
                "tool_type": tool_type
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

    async def _regenerate_rejected_items(
        self,
        tool_operation_id: str,
        regenerate_count: int,
        analysis: Dict,
        **kwargs
    ) -> Dict:
        """Handle item regeneration after partial approval"""
        try:
            # Get operation and tool info
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            tool_type = operation.get('tool_type')
            tool = self.orchestrator.tools.get(tool_type)  # We'll need to inject orchestrator
            if not tool:
                raise ValueError(f"Tool not found for type {tool_type}")

            # Get generation parameters from original command
            topic = operation.get("input_data", {}).get("command_info", {}).get("topic")
            if not topic:
                raise ValueError("Could not find topic for regeneration")

            logger.info(f"Regenerating {regenerate_count} items for operation {tool_operation_id}")

            # Use tool's _generate_content function
            generation_result = await tool._generate_content(
                topic=topic,
                count=regenerate_count,
                schedule_id=operation.get("input_data", {}).get("schedule_id"),
                tool_operation_id=tool_operation_id
            )

            # Create regeneration items through tool state manager
            items = await self.tool_state_manager.create_regeneration_items(
                session_id=operation['session_id'],
                tool_operation_id=tool_operation_id,
                items_data=generation_result["items"],
                content_type=operation['metadata']['content_type'],
                schedule_id=generation_result.get("schedule_id")
            )

            return {
                "items": items,
                "schedule_id": generation_result.get("schedule_id"),
                "tool_operation_id": tool_operation_id,
                "regeneration_needed": True,
                "regenerate_count": len(items)
            }

        except Exception as e:
            logger.error(f"Error regenerating items: {e}")
            return self.analyzer.create_error_response(str(e))