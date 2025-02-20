from enum import Enum
from typing import Dict, Optional, Any, List
from datetime import datetime, UTC
import logging
from src.db.db_schema import RinDB, ToolOperation, ToolOperationState, OperationStatus
from src.utils.trigger_detector import TriggerDetector
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)

class ToolStateManager:
    def __init__(self, db: RinDB, schedule_service=None):
        """Initialize tool state manager with database connection"""
        logger.info("Initializing ToolStateManager...")
        if not db:
            logger.error("Database instance is None!")
            raise ValueError("Database instance is required")
        if not isinstance(db, RinDB):
            logger.error(f"Expected RinDB instance, got {type(db)}")
            raise ValueError(f"Database must be RinDB instance, got {type(db)}")
        self.db = db
        self.schedule_service = schedule_service
        self.operations = {}
        self.trigger_detector = TriggerDetector()  # Initialize the trigger detector
        logger.info("ToolStateManager initialized with database connection")

        # Updated state transitions to allow COLLECTING -> APPROVING
        self.valid_transitions = {
            ToolOperationState.INACTIVE.value: [
                ToolOperationState.COLLECTING.value
            ],
            ToolOperationState.COLLECTING.value: [
                ToolOperationState.APPROVING.value,
                ToolOperationState.ERROR.value,
                ToolOperationState.CANCELLED.value
            ],
            ToolOperationState.APPROVING.value: [
                ToolOperationState.EXECUTING.value,  # For approved items
                ToolOperationState.COLLECTING.value, # For items needing regeneration
                ToolOperationState.ERROR.value,
                ToolOperationState.CANCELLED.value
            ],
            ToolOperationState.EXECUTING.value: [
                ToolOperationState.COMPLETED.value,
                ToolOperationState.CANCELLED.value,
                ToolOperationState.ERROR.value
            ]
        }

    async def start_operation(
        self,
        session_id: str,
        operation_type: str,
        initial_data: Optional[Dict[str, Any]] = None
    ) -> Dict:
        """Start any tool operation with a unique ID"""
        try:
            tool_operation_id = str(ObjectId())
            requires_approval = initial_data.get("requires_approval", True)
            
            operation_data = {
                "_id": ObjectId(tool_operation_id),
                "session_id": session_id,
                "tool_type": operation_type,
                "state": ToolOperationState.COLLECTING.value,
                "step": "analyzing",
                "input_data": {
                    "command": initial_data.get("command"),
                    "status": initial_data.get("status"),
                    "operation_metadata": initial_data.get("operation_metadata", {})
                },
                "output_data": {
                    "status": OperationStatus.PENDING.value,
                    "content": [],
                    "requires_approval": requires_approval,
                    "pending_items": [],
                    "approved_items": [],
                    "rejected_items": []
                },
                "metadata": {
                    "state_history": [{
                        "state": ToolOperationState.COLLECTING.value,
                        "step": "analyzing",
                        "timestamp": datetime.now(UTC).isoformat()
                    }],
                    "item_states": {}
                },
                "created_at": datetime.now(UTC),
                "last_updated": datetime.now(UTC)
            }
            
            # Create new operation
            result = await self.db.tool_operations.insert_one(operation_data)
            operation_data['_id'] = result.inserted_id
            logger.info(f"Started {operation_type} operation {tool_operation_id} for session {session_id}")
            return operation_data

        except Exception as e:
            logger.error(f"Error starting operation: {e}")
            return None

    async def update_operation(
        self,
        session_id: str,
        tool_operation_id: str,  # Now required
        state: str = None,
        step: str = None,
        metadata: Dict = None,
        content_updates: Dict = None
    ) -> bool:
        """Update tool operation state with operation ID"""
        try:
            # Fetch current operation by ID and session
            current_op = await self.db.tool_operations.find_one({
                "_id": ObjectId(tool_operation_id),
                "session_id": session_id
            })
            
            if not current_op:
                logger.error(f"No operation found for ID {tool_operation_id} and session {session_id}")
                return False

            # Build update data
            update_data = {"last_updated": datetime.now(UTC)}
            
            if state:
                current_state = current_op.get("state")
                if not self._is_valid_transition(current_state, state):
                    logger.warning(
                        f"Invalid state transition from {current_state} to {state}. "
                        f"Valid transitions are: {self.valid_transitions.get(current_state, [])}"
                    )
                    return False
                update_data["state"] = state
                
            if step:
                update_data["step"] = step

            if content_updates:
                # Merge with existing output_data
                existing_output = current_op.get("output_data", {})
                update_data["output_data"] = {
                    **existing_output,
                    **content_updates
                }

            if metadata:
                # Merge with existing metadata
                existing_metadata = current_op.get("metadata", {})
                update_data["metadata"] = {
                    **existing_metadata,
                    **metadata,
                    "last_modified": datetime.now(UTC).isoformat()
                }

            # Update operation by ID
            result = await self.db.tool_operations.find_one_and_update(
                {"_id": ObjectId(tool_operation_id)},
                {"$set": update_data},
                return_document=True
            )
            
            return bool(result)

        except Exception as e:
            logger.error(f"Error updating operation: {e}")
            return False

    async def get_operation(self, session_id: str) -> Optional[ToolOperation]:
        """Get current operation state"""
        return await self.db.get_tool_operation_state(session_id)

    async def end_operation(
        self,
        session_id: str,
        tool_operation_id: str,  # Now required
        status: OperationStatus,
        reason: str = None,
        api_response: Dict = None,
        requires_approval: bool = True,
        is_scheduled: bool = False,
        metadata: Dict = None
    ) -> bool:
        """End operation with proper state transition"""
        try:
            current_op = await self.db.tool_operations.find_one({
                "_id": ObjectId(tool_operation_id),
                "session_id": session_id
            })
            
            if not current_op:
                return False

            current_state = current_op.get("state")
            final_state = self._get_final_state(current_state, status)

            # Update operation with final state
            operation_data = {
                "state": final_state,
                "step": self._get_step_for_state(final_state),
                "output_data": {
                    **(current_op.get("output_data", {})),
                    "status": status.value,
                    "api_response": api_response
                },
                "metadata": {
                    **(current_op.get("metadata", {})),
                    "end_time": datetime.now(UTC).isoformat(),
                    "end_reason": reason,
                    "final_status": status.value,
                    "requires_approval": requires_approval,
                    "is_scheduled": is_scheduled
                },
                "last_updated": datetime.now(UTC)
            }

            return await self.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                state=final_state,
                metadata=operation_data.get("metadata", {})
            )

        except Exception as e:
            logger.error(f"Error ending operation: {e}")
            return False

    def _is_valid_transition(self, current_state: str, new_state: str) -> bool:
        """Check if state transition is valid"""
        try:
            # Normalize states to lowercase for comparison
            current = current_state.lower() if current_state else 'inactive'
            new = new_state.lower() if new_state else 'inactive'
            
            # Get valid transitions for current state
            valid_transitions = self.valid_transitions.get(current, [])
            
            if new not in valid_transitions:
                logger.warning(
                    f"Invalid state transition attempted: {current} -> {new}. "
                    f"Valid transitions are: {valid_transitions}"
                )
                return False
            
            logger.info(f"Valid state transition: {current} -> {new}")
            return True
        
        except Exception as e:
            logger.error(f"Error checking state transition: {e}")
            return False

    def _get_step_for_state(self, state: ToolOperationState) -> str:
        """Get appropriate step name for state"""
        step_mapping = {
            ToolOperationState.INACTIVE: "inactive",
            ToolOperationState.COLLECTING: "collecting",
            ToolOperationState.APPROVING: "awaiting_approval",
            ToolOperationState.EXECUTING: "executing",
            ToolOperationState.COMPLETED: "completed",
            ToolOperationState.CANCELLED: "cancelled",
            ToolOperationState.ERROR: "error"
        }
        return step_mapping.get(state, "unknown")

    def _get_final_state(self, current_state: str, status: OperationStatus) -> str:
        """Determine final state based on current state and status"""
        if status == OperationStatus.APPROVED:
            return ToolOperationState.COMPLETED.value
        elif status == OperationStatus.FAILED:
            return ToolOperationState.ERROR.value
        elif status == OperationStatus.REJECTED:
            return ToolOperationState.CANCELLED.value
        else:
            logger.warning(f"Unhandled status {status} in state {current_state}")
            return ToolOperationState.ERROR.value

    async def get_operation_state(self, session_id: str) -> Optional[Dict]:
        """Get current operation state"""
        try:
            return await self.db.get_tool_operation_state(session_id)
        except Exception as e:
            logger.error(f"Error getting operation state: {e}")
            return None

    async def validate_operation_items(self, tool_operation_id: str) -> bool:
        """Validate all items are properly linked to operation"""
        try:
            operation = await self.db.tool_operations.find_one({"_id": ObjectId(tool_operation_id)})
            if not operation:
                return False

            # Get all items for this operation
            items = await self.db.tool_items.find({
                "tool_operation_id": tool_operation_id
            }).to_list(None)

            # Validate items match operation's pending_items
            pending_ids = set(operation["output_data"]["pending_items"])
            item_ids = {str(item["_id"]) for item in items}
            
            if pending_ids != item_ids:
                logger.error(f"Mismatch in operation items. Expected: {pending_ids}, Found: {item_ids}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error validating operation items: {e}")
            return False

    async def get_operation_by_id(self, tool_operation_id: str) -> Optional[Dict]:
        """Get operation by ID"""
        try:
            operation = await self.db.tool_operations.find_one({"_id": ObjectId(tool_operation_id)})
            return operation
        except Exception as e:
            logger.error(f"Error getting operation by ID: {e}")
            return None

    async def update_operation_items(
        self,
        tool_operation_id: str,
        item_ids: List[str],
        new_state: str,
        new_status: str
    ) -> bool:
        """Update state and status for specific items in an operation"""
        try:
            result = await self.db.tool_items.update_many(
                {
                    "_id": {"$in": [ObjectId(id) for id in item_ids]},
                    "tool_operation_id": tool_operation_id
                },
                {
                    "$set": {
                        "state": new_state,
                        "status": new_status,
                        "last_updated": datetime.now(UTC)
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating operation items: {e}")
            return False

    async def get_operation_items(
        self,
        tool_operation_id: str,
        state: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict]:
        """Get items for an operation with optional state/status filters"""
        try:
            query = {"tool_operation_id": tool_operation_id}
            if state:
                query["state"] = state
            if status:
                query["status"] = status
            
            return await self.db.tool_items.find(query).to_list(None)
        except Exception as e:
            logger.error(f"Error getting operation items: {e}")
            return []

    async def update_operation_state(
        self,
        tool_operation_id: str,
        item_updates: Optional[List[Dict]] = None
    ) -> bool:
        """Update operation status based on aggregate item states and scheduling"""
        try:
            operation = await self.get_operation_by_id(tool_operation_id)
            if not operation:
                return False

            # Get all items if no updates provided
            items = item_updates or await self.get_operation_items(tool_operation_id)
            
            # Get scheduling info from operation metadata
            is_scheduled_operation = operation.get('metadata', {}).get('requires_scheduling', False)
            expected_item_count = operation.get('metadata', {}).get('expected_item_count', len(items))
            
            # Count items by state and status
            executing_items = [i for i in items if i['state'] == ToolOperationState.EXECUTING.value]
            completed_items = [i for i in items if i['state'] == ToolOperationState.COMPLETED.value]
            scheduled_items = [i for i in items if i['status'] == OperationStatus.SCHEDULED.value]
            executed_items = [i for i in items if i['status'] == OperationStatus.EXECUTED.value]
            
            # Determine item state completeness
            all_items_approved = len(executing_items) == expected_item_count
            all_items_scheduled = is_scheduled_operation and len(scheduled_items) == expected_item_count
            all_items_executed = is_scheduled_operation and len(executed_items) == expected_item_count
            
            # Determine new operation state based on scheduling requirements
            if is_scheduled_operation:
                if all_items_executed:
                    new_state = ToolOperationState.COMPLETED.value
                elif all_items_scheduled:
                    new_state = ToolOperationState.EXECUTING.value
                elif all_items_approved:
                    # Items approved but not yet scheduled
                    new_state = ToolOperationState.APPROVING.value
                else:
                    # Still in approval/scheduling process
                    return True
            else:
                # Non-scheduled operation logic
                if all_items_approved:
                    new_state = ToolOperationState.EXECUTING.value
                elif len(executing_items) + len(completed_items) == expected_item_count:
                    new_state = ToolOperationState.COMPLETED.value
                else:
                    return True

            # Update operation state with detailed metadata
            await self.db.tool_operations.update_one(
                {"_id": ObjectId(tool_operation_id)},
                {
                    "$set": {
                        "state": new_state,
                        "metadata": {
                            **(operation.get('metadata', {})),
                            "item_summary": {
                                "total_items": expected_item_count,
                                "executing_count": len(executing_items),
                                "completed_count": len(completed_items),
                                "scheduled_count": len(scheduled_items),
                                "executed_count": len(executed_items),
                                "requires_scheduling": is_scheduled_operation,
                                "last_state_update": datetime.now(UTC).isoformat()
                            }
                        },
                        "last_updated": datetime.now(UTC)
                    }
                }
            )
            
            logger.info(
                f"Operation {tool_operation_id} state updated to {new_state}. "
                f"Items: {len(executing_items)} executing, {len(scheduled_items)} scheduled, "
                f"{len(executed_items)} executed"
            )
            
            return True

        except Exception as e:
            logger.error(f"Error updating operation state: {e}")
            return False

    def _determine_operation_status(self, item_states: set) -> str:
        """Determine operation status based on item states"""
        # If any items are still processing, operation remains PENDING
        if any(state in {
            ToolOperationState.COLLECTING.value,
            ToolOperationState.APPROVING.value,
            ToolOperationState.EXECUTING.value
        } for state in item_states):
            return OperationStatus.PENDING.value
            
        # All items must be in the same final state
        if all(state == ToolOperationState.COMPLETED.value for state in item_states):
            return OperationStatus.EXECUTED.value
        elif all(state == ToolOperationState.CANCELLED.value for state in item_states):
            return OperationStatus.REJECTED.value
        elif all(state == ToolOperationState.ERROR.value for state in item_states):
            return OperationStatus.FAILED.value
            
        # Default to PENDING if mixed states
        return OperationStatus.PENDING.value

    async def sync_items_to_operation_status(
        self,
        tool_operation_id: str,
        operation_status: str
    ) -> None:
        """Sync all items to match operation status"""
        status_to_state_map = {
            OperationStatus.APPROVED.value: ToolOperationState.EXECUTING.value,
            OperationStatus.SCHEDULED.value: ToolOperationState.EXECUTING.value,
            OperationStatus.EXECUTED.value: ToolOperationState.COMPLETED.value,
            OperationStatus.REJECTED.value: ToolOperationState.CANCELLED.value,
            OperationStatus.FAILED.value: ToolOperationState.ERROR.value
        }
        
        if operation_status in status_to_state_map:
            new_state = status_to_state_map[operation_status]
            await self.db.tool_items.update_many(
                {"tool_operation_id": tool_operation_id},
                {
                    "$set": {
                        "state": new_state,
                        "last_updated": datetime.now(UTC)
                    }
                }
            )

    async def create_tool_items(
        self,
        session_id: str,
        tool_operation_id: str,
        items_data: List[Dict],
        content_type: str,
        schedule_id: Optional[str] = None,
        initial_state: str = ToolOperationState.COLLECTING.value,
        initial_status: str = OperationStatus.PENDING.value
    ) -> List[Dict]:
        """Create new tool items with proper state tracking"""
        try:
            # Validate operation exists
            operation = await self.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            saved_items = []
            for item in items_data:
                tool_item = {
                    "session_id": session_id,
                    "tool_operation_id": tool_operation_id,
                    "schedule_id": schedule_id,
                    "content_type": content_type,
                    "state": initial_state,
                    "status": initial_status,
                    "content": {
                        "raw_content": item.get("content"),
                        "formatted_content": item.get("content"),
                        "version": "1.0"
                    },
                    "metadata": {
                        **item.get("metadata", {}),
                        "created_at": datetime.now(UTC).isoformat(),
                        "state_history": [{
                            "state": initial_state,
                            "status": initial_status,
                            "timestamp": datetime.now(UTC).isoformat()
                        }]
                    }
                }
                
                result = await self.db.tool_items.insert_one(tool_item)
                saved_item = {**tool_item, "_id": str(result.inserted_id)}
                saved_items.append(saved_item)
                
                logger.info(f"Created tool item {saved_item['_id']} in {initial_state} state")

            # Update operation's pending items
            await self.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                content_updates={
                    "pending_items": [str(item["_id"]) for item in saved_items]
                }
            )

            return saved_items

        except Exception as e:
            logger.error(f"Error creating tool items: {e}")
            raise

    async def create_regeneration_items(
        self,
        session_id: str,
        tool_operation_id: str,
        items_data: List[Dict],
        content_type: str,
        schedule_id: Optional[str] = None
    ) -> List[Dict]:
        """Create new items specifically for regeneration"""
        try:
            # Validate operation exists and is in valid state
            operation = await self.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")
            
            if operation['state'] not in [
                ToolOperationState.APPROVING.value,
                ToolOperationState.COLLECTING.value
            ]:
                raise ValueError(f"Operation in invalid state for regeneration: {operation['state']}")

            # Create items starting in COLLECTING state
            items = await self.create_tool_items(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                items_data=items_data,
                content_type=content_type,
                schedule_id=schedule_id,
                initial_state=ToolOperationState.COLLECTING.value,
                initial_status=OperationStatus.PENDING.value
            )

            # Update operation metadata
            await self.update_operation(
                session_id=session_id,
                tool_operation_id=tool_operation_id,
                metadata={
                    "regeneration_count": len(items),
                    "last_regeneration": datetime.now(UTC).isoformat()
                }
            )

            return items

        except Exception as e:
            logger.error(f"Error creating regeneration items: {e}")
            raise