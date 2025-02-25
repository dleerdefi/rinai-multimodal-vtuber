from typing import Dict, Optional, Any, List
from datetime import datetime, UTC
import logging
from bson.objectid import ObjectId
from src.db.enums import (
    OperationStatus,
    ToolOperationState,
    ScheduleState
)
from src.db.db_schema import RinDB, ToolOperation
from src.utils.trigger_detector import TriggerDetector

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

        # Define valid state transitions
        self.valid_transitions = {
            ToolOperationState.INACTIVE.value: [
                ToolOperationState.COLLECTING.value
            ],
            ToolOperationState.COLLECTING.value: [
                ToolOperationState.APPROVING.value,
                ToolOperationState.EXECUTING.value,
                ToolOperationState.ERROR.value  # Allow error from collecting
            ],
            ToolOperationState.APPROVING.value: [
                ToolOperationState.EXECUTING.value,
                ToolOperationState.CANCELLED.value,
                ToolOperationState.ERROR.value  # Allow error from approving
            ],
            ToolOperationState.EXECUTING.value: [
                ToolOperationState.COMPLETED.value,
                ToolOperationState.ERROR.value
            ],
            ToolOperationState.COMPLETED.value: [],  # Terminal state
            ToolOperationState.ERROR.value: [],      # Terminal state
            ToolOperationState.CANCELLED.value: []   # Terminal state
        }

    async def start_operation(
        self,
        session_id: str,
        tool_type: str,
        initial_data: Optional[Dict[str, Any]] = None
    ) -> Dict:
        """Start any tool operation with a unique ID"""
        try:
            tool_operation_id = str(ObjectId())
            initial_data = initial_data or {}
            
            # Get tool registry settings if available
            tool_registry = initial_data.get("tool_registry", {})
            
            # Set defaults from registry or fallback to provided values
            requires_approval = tool_registry.get("requires_approval", initial_data.get("requires_approval", True))
            requires_scheduling = tool_registry.get("requires_scheduling", initial_data.get("requires_scheduling", False))
            content_type = tool_registry.get("content_type", initial_data.get("content_type"))
            
            operation_data = {
                "_id": ObjectId(tool_operation_id),
                "session_id": session_id,
                "tool_type": tool_type,
                "state": ToolOperationState.COLLECTING.value,
                "step": "analyzing",
                "input_data": {
                    "command": initial_data.get("command"),
                    "status": initial_data.get("status"),
                    "operation_metadata": initial_data.get("operation_metadata", {}),
                    "schedule_info": initial_data.get("schedule_info")
                },
                "output_data": {
                    "status": OperationStatus.PENDING.value,
                    "content": [],
                    "requires_approval": requires_approval,
                    "pending_items": [],
                    "approved_items": [],
                    "rejected_items": [],
                    "schedule_id": None
                },
                "metadata": {
                    "state_history": [{
                        "state": ToolOperationState.COLLECTING.value,
                        "step": "analyzing",
                        "timestamp": datetime.now(UTC).isoformat()
                    }],
                    "item_states": {},
                    "requires_scheduling": requires_scheduling,
                    "content_type": content_type,
                    "generation_phase": "initializing",
                    "schedule_state": ScheduleState.PENDING.value if requires_scheduling else None
                },
                "created_at": datetime.now(UTC),
                "last_updated": datetime.now(UTC)
            }
            
            # Create new operation
            result = await self.db.tool_operations.insert_one(operation_data)
            operation_data['_id'] = result.inserted_id
            
            logger.info(f"Started {tool_type} operation {tool_operation_id} for session {session_id}")
            return operation_data

        except Exception as e:
            logger.error(f"Error starting operation: {e}")
            raise

    async def update_operation(
        self,
        session_id: str,
        tool_operation_id: str,
        state: Optional[str] = None,
        step: Optional[str] = None,
        content_updates: Optional[Dict] = None,
        metadata: Optional[Dict] = None,
        input_data: Optional[Dict] = None,
        output_data: Optional[Dict] = None
    ) -> bool:
        """Update tool operation with new data"""
        try:
            # Validate operation exists and belongs to session
            current_op = await self.db.tool_operations.find_one({
                "_id": ObjectId(tool_operation_id),
                "session_id": session_id
            })
            
            if not current_op:
                logger.error(f"No operation found for ID {tool_operation_id} and session {session_id}")
                return False

            update_data = {"last_updated": datetime.now(UTC)}
            
            # Validate state transition if state is being updated
            if state:
                current_state = current_op.get("state")
                if not self._is_valid_transition(current_state, state):
                    logger.warning(
                        f"Invalid state transition from {current_state} to {state}. "
                        f"Valid transitions are: {self.valid_transitions.get(current_state, [])}"
                    )
                    return False
                update_data["state"] = state
                
                # Handle schedule state updates if this is a scheduled operation
                if current_op.get("metadata", {}).get("requires_scheduling"):
                    schedule_id = current_op.get("output_data", {}).get("schedule_id")
                    if schedule_id:
                        if state == ToolOperationState.COMPLETED.value:
                            await self.db.update_schedule_state(
                                schedule_id=schedule_id,
                                state=ScheduleState.ACTIVE.value
                            )
                        elif state in [ToolOperationState.CANCELLED.value, ToolOperationState.ERROR.value]:
                            await self.db.update_schedule_state(
                                schedule_id=schedule_id,
                                state=ScheduleState.CANCELLED.value if state == ToolOperationState.CANCELLED.value else ScheduleState.ERROR.value
                            )
                
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

            if input_data:
                # Merge with existing input_data
                existing_input = current_op.get("input_data", {})
                update_data["input_data"] = {
                    **existing_input,
                    **input_data
                }

            if output_data:
                # Merge with existing output_data if not already updated
                if "output_data" not in update_data:
                    existing_output = current_op.get("output_data", {})
                    update_data["output_data"] = {
                        **existing_output,
                        **output_data
                    }
                
            # Update operation
            result = await self.db.tool_operations.update_one(
                {
                    "_id": ObjectId(tool_operation_id),
                    "session_id": session_id
                },
                {"$set": update_data}
            )
            
            return result.modified_count > 0

        except Exception as e:
            logger.error(f"Error updating operation: {e}")
            return False

    async def get_operation(self, session_id: str) -> Optional[ToolOperation]:
        """Get current operation state"""
        return await self.db.get_tool_operation_state(session_id)

    async def end_operation(
        self,
        session_id: str,
        success: bool = True,
        api_response: Optional[Dict] = None
    ) -> bool:
        """End tool operation with appropriate state/status based on operation type"""
        try:
            operation = await self.get_operation(session_id)
            if not operation:
                return False

            # Get operation characteristics
            requires_approval = operation.get("metadata", {}).get("requires_approval", False)
            requires_scheduling = operation.get("metadata", {}).get("requires_scheduling", False)
            current_state = operation.get("state")
            current_status = operation.get("status")

            # Determine final states based on operation type and success
            final_state = self._determine_final_state(
                success=success,
                current_state=current_state
            )

            final_status = self._determine_final_status(
                success=success,
                requires_scheduling=requires_scheduling,
                current_status=current_status
            )

            # Update operation with final state/status
            return await self.update_operation(
                session_id=session_id,
                tool_operation_id=str(operation["_id"]),
                state=final_state,
                output_data={
                    "status": final_status,
                    "api_response": api_response
                },
                metadata={
                    "completion_time": datetime.now(UTC).isoformat(),
                    "final_status": "success" if success else "error"
                }
            )

        except Exception as e:
            logger.error(f"Error ending operation: {e}")
            return False

    def _determine_final_state(self, success: bool, current_state: str) -> str:
        """Determine final ToolOperationState based on success and current state"""
        if not success:
            return ToolOperationState.ERROR.value
            
        if current_state == ToolOperationState.CANCELLED.value:
            return ToolOperationState.CANCELLED.value
            
        return ToolOperationState.COMPLETED.value

    def _determine_final_status(
        self,
        success: bool,
        requires_scheduling: bool,
        current_status: str
    ) -> str:
        """Determine final OperationStatus based on operation type and success"""
        if not success:
            return OperationStatus.FAILED.value
            
        if current_status == OperationStatus.REJECTED.value:
            return OperationStatus.REJECTED.value
            
        if requires_scheduling:
            return OperationStatus.SCHEDULED.value
            
        return OperationStatus.EXECUTED.value

    def _is_valid_transition(self, current_state: str, new_state: str) -> bool:
        """Check if state transition is valid"""
        if current_state == new_state:
            return True
        
        if current_state not in self.valid_transitions:
            logger.error(f"Invalid current state: {current_state}")
            return False
        
        valid_next_states = self.valid_transitions[current_state]
        is_valid = new_state in valid_next_states
        
        if not is_valid:
            logger.warning(
                f"Invalid state transition from {current_state} to {new_state}. "
                f"Valid transitions are: {valid_next_states}"
            )
        
        return is_valid

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

    async def update_operation_state(self, tool_operation_id: str, item_updates: Optional[List[Dict]] = None) -> bool:
        """Update operation state based on item states and scheduling requirements"""
        try:
            operation = await self.get_operation_by_id(tool_operation_id)
            if not operation:
                return False

            # Get operation characteristics
            is_one_shot = not (
                operation.get('metadata', {}).get('requires_approval', False) or 
                operation.get('metadata', {}).get('requires_scheduling', False)
            )
            current_state = operation.get('state')

            # For one-shot tools, transition directly to COMPLETED
            if is_one_shot and current_state == ToolOperationState.COLLECTING.value:
                new_state = ToolOperationState.COMPLETED.value
                await self.db.tool_operations.update_one(
                    {"_id": ObjectId(tool_operation_id)},
                    {
                        "$set": {
                            "state": new_state,
                            "status": OperationStatus.EXECUTED.value,
                            "metadata.last_state_update": datetime.now(UTC).isoformat()
                        }
                    }
                )
                return True

            # Regular state progression for non-one-shot tools
            items = item_updates or await self.get_operation_items(tool_operation_id)
            is_scheduled_operation = operation.get('metadata', {}).get('requires_scheduling', False)

            # Count items by state
            items_by_state = {
                'collecting': [i for i in items if i['state'] == ToolOperationState.COLLECTING.value],
                'approving': [i for i in items if i['state'] == ToolOperationState.APPROVING.value],
                'executing': [i for i in items if i['state'] == ToolOperationState.EXECUTING.value],
                'completed': [i for i in items if i['state'] == ToolOperationState.COMPLETED.value]
            }

            # Count items by status
            items_by_status = {
                'pending': [i for i in items if i['status'] == OperationStatus.PENDING.value],
                'approved': [i for i in items if i['status'] == OperationStatus.APPROVED.value],
                'scheduled': [i for i in items if i['status'] == OperationStatus.SCHEDULED.value],
                'executed': [i for i in items if i['status'] == OperationStatus.EXECUTED.value]
            }

            new_state = current_state
            expected_item_count = operation.get('metadata', {}).get('expected_item_count', len(items))

            # Determine new state based on operation type and item states
            if is_scheduled_operation:
                if len(items_by_status['executed']) == expected_item_count:
                    new_state = ToolOperationState.COMPLETED.value
                elif len(items_by_status['scheduled']) == expected_item_count:
                    new_state = ToolOperationState.EXECUTING.value  # Schedule is active
                elif len(items_by_status['approved']) == expected_item_count:
                    # All items approved but not yet scheduled
                    new_state = ToolOperationState.APPROVING.value
            else:
                # Non-scheduled operation state progression
                if len(items_by_state['completed']) == expected_item_count:
                    new_state = ToolOperationState.COMPLETED.value
                elif len(items_by_state['executing']) == expected_item_count:
                    new_state = ToolOperationState.EXECUTING.value

            # Only update if state has changed
            if new_state != current_state:
                await self.db.tool_operations.update_one(
                    {"_id": ObjectId(tool_operation_id)},
                    {
                        "$set": {
                            "state": new_state,
                            "metadata.item_summary": {
                                "total_items": expected_item_count,
                                "by_state": {state: len(items) for state, items in items_by_state.items()},
                                "by_status": {status: len(items) for status, items in items_by_status.items()},
                                "requires_scheduling": is_scheduled_operation,
                                "last_state_update": datetime.now(UTC).isoformat()
                            }
                        }
                    }
                )
                
                logger.info(
                    f"Operation {tool_operation_id} state updated: {current_state} -> {new_state}. "
                    f"Scheduled: {len(items_by_status['scheduled'])}, "
                    f"Executed: {len(items_by_status['executed'])}, "
                    f"Total: {expected_item_count}"
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

    async def create_operation(
        self,
        session_id: str,
        tool_type: str,
        state: str,
        step: str = None,
        metadata: Dict = None
    ) -> Dict:
        """Create new tool operation"""
        operation_data = {
            "session_id": session_id,
            "tool_type": tool_type,
            "state": state,
            "step": step,
            "created_at": datetime.now(UTC),
            "metadata": metadata or {}
        }
        
        result = await self.db.set_tool_operation_state(
            session_id=session_id,
            operation_data=operation_data
        )
        
        if not result:
            raise ValueError(f"Failed to create operation for session {session_id}")
        
        return result