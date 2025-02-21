from datetime import datetime, UTC, timedelta
import logging
from typing import Dict, List, Optional, Any, Union
import json
from bson import ObjectId

from src.tools.base import (
    BaseTool,
    AgentResult,
    AgentDependencies,
    CommandAnalysis,
    ToolOperation,
    ToolRegistry
)
from src.managers.tool_state_manager import ToolStateManager
from src.services.llm_service import LLMService, ModelType
from src.db.mongo_manager import MongoManager
from src.db.db_schema import (
    ScheduledOperation,
    OperationStatus,
    ContentType,
    ToolType,
    ToolOperationState,
    OperationMetadata,
    ToolItem,
    TwitterContent,
    TwitterParams,
    TwitterMetadata,
    TwitterResponse,
    ToolItemContent,
    ToolItemParams,
    ToolItemMetadata,
    ToolItemResponse,
    TweetGenerationResponse,
    TwitterCommandAnalysis,
    ScheduleState
)
from src.utils.json_parser import parse_strict_json
from src.managers.approval_manager import ApprovalManager, ApprovalAction, ApprovalState
from src.managers.schedule_manager import ScheduleManager

logger = logging.getLogger(__name__)

class TwitterTool(BaseTool):
    name = "twitter"
    description = "Posts and manages tweets"
    version = "1.0"
    registry = ToolRegistry(
        content_type=ContentType.TWEET,
        tool_type=ToolType.TWITTER,
        requires_approval=True,
        requires_scheduling=True,
        required_clients=["twitter_client"],
        required_managers=[
            "tool_state_manager",
            "approval_manager",
            "schedule_manager"
        ]
    )

    def __init__(self, 
                 deps: Optional[AgentDependencies] = None, 
                 tool_state_manager: Optional[ToolStateManager] = None,
                 llm_service: Optional[LLMService] = None,
                 approval_manager: Optional[ApprovalManager] = None,
                 schedule_manager: Optional[ScheduleManager] = None):
        """Initialize tweet tool with dependencies and services"""
        super().__init__()
        self.deps = deps or AgentDependencies()
        self.tool_state_manager = tool_state_manager
        self.llm_service = llm_service or LLMService()
        self.approval_manager = approval_manager or ApprovalManager(
            tool_state_manager=tool_state_manager,
            db=tool_state_manager.db if tool_state_manager else None,
            llm_service=llm_service
        )
        self.db = tool_state_manager.db if tool_state_manager else None
        self.schedule_manager = schedule_manager or ScheduleManager(
            tool_state_manager=tool_state_manager,
            db=self.db,
            tool_registry={ContentType.TWEET.value: self}
        )

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by tweet tool"""
        return isinstance(input_data, str)  # Basic type check only

    async def run(self, input_data: str) -> Dict:
        """Run the tweet tool"""
        try:
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            
            if not operation or operation.get('state') == ToolOperationState.COMPLETED.value:
                # Initial tweet generation flow
                command_info = await self._analyze_twitter_command(input_data)
                generation_result = await self._generate_tweets(
                    topic=command_info["topic"],
                    count=command_info["item_count"],
                    schedule_id=command_info["schedule_id"],
                    tool_operation_id=command_info["tool_operation_id"]
                )
                return await self.approval_manager.start_approval_flow(
                    session_id=self.deps.session_id,
                    tool_operation_id=command_info["tool_operation_id"],
                    items=generation_result["items"]
                )
            else:
                # Get current items for the operation
                current_items = await self.db.tool_items.find({
                    "tool_operation_id": str(operation['_id']),
                    "state": ToolOperationState.APPROVING.value
                }).to_list(None)

                # Handle approval response through ApprovalManager
                result = await self.approval_manager.process_approval_response(
                    message=input_data,
                    session_id=self.deps.session_id,
                    content_type=ContentType.TWEET.value,
                    tool_operation_id=str(operation['_id']),
                    handlers={
                        ApprovalAction.PARTIAL_APPROVAL.value: self._regenerate_rejected_tweets,
                        ApprovalAction.REGENERATE_ALL.value: self._regenerate_rejected_tweets,
                        ApprovalAction.FULL_APPROVAL.value: lambda tool_operation_id, session_id, analysis, **kwargs:
                            self.approval_manager._handle_full_approval(
                                tool_operation_id=tool_operation_id,
                                session_id=session_id,
                                items=current_items,  # Pass the current items
                                analysis=analysis
                            ),
                        ApprovalAction.EXIT.value: self.approval_manager.handle_exit
                    }
                )

                # If regeneration happened, start new approval flow with the generated items
                if result.get("items"):  # Check for items from _regenerate_rejected_tweets
                    return await self.approval_manager.start_approval_flow(
                        session_id=self.deps.session_id,
                        tool_operation_id=str(operation['_id']),
                        items=result["items"],
                        analysis=result.get("analysis")
                    )

                return result

        except Exception as e:
            logger.error(f"Error in tweet tool: {e}", exc_info=True)
            return self.approval_manager.analyzer.create_error_response(str(e))

    async def _analyze_twitter_command(self, command: str) -> Dict:
        """Analyze command and setup initial schedule"""
        try:
            logger.info(f"Starting command analysis for: {command}")
            
            # Start operation through manager
            operation = await self.tool_state_manager.start_operation(
                session_id=self.deps.session_id,
                operation_type=ToolType.TWITTER.value,
                initial_data={
                    "command": command,
                    "requires_approval": True,
                    "content_type": ContentType.TWEET.value
                }
            )
            tool_operation_id = str(operation['_id'])
            
            # Get LLM analysis
            prompt = f"""You are a Twitter action analyzer. Determine the specific Twitter action needed.

Command: "{command}"

Available Twitter actions: 
1. send_item: Post a new tweet immediately
   Parameters: message, account_id (optional)

2. schedule_items: Schedule one or more tweets for later
   Parameters: 
   - item_count: number of tweets to schedule
   - topic: what to tweet about
   - schedule_type: "one_time"
   - schedule_time: when to post (specify "spread_24h" or specific time)
   - interval_minutes: minutes between tweets (if spreading)
   - start_time: when to start posting (ISO format, if specific time)
   - approval_required: true

Instructions:
- Return ONLY valid JSON matching the example format
- Extract count, topic, and ALL timing information from command
- For spread_24h, calculate appropriate interval based on tweet count
- For specific times, provide start_time in ISO format
- Include ALL scheduling parameters

Example response format:
{{
    "tools_needed": [{{
        "tool_name": "twitter",
        "action": "schedule_items",
        "parameters": {{
            "item_count": 5,
            "topic": "artificial intelligence",
            "schedule_type": "one_time",
            "schedule_time": "spread_24h",
            "interval_minutes": 288,  # Calculated for 5 tweets over 24h
            "approval_required": true
        }},
        "priority": 1
    }}],
    "reasoning": "User requested scheduling multiple tweets about AI spread over 24 hours"
}}"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a precise Twitter action analyzer. Return ONLY valid JSON with no additional text."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            # Log the prompt being sent
            logger.info(f"Sending prompt to LLM: {messages}")

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 150
                }
            )
            
            logger.info(f"Raw LLM response: {response}")
            
            try:
                # Parse response and extract key parameters
                parsed_data = json.loads(response)
                logger.info(f"Parsed JSON data: {parsed_data}")
                
                tools_data = parsed_data.get("tools_needed", [{}])[0]
                logger.info(f"Extracted tools_data: {tools_data}")
                
                params = tools_data.get("parameters", {})
                logger.info(f"Extracted parameters: {params}")
                
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse LLM response as JSON: {e}")
                logger.error(f"Raw response that failed parsing: {response}")
                raise
            except Exception as e:
                logger.error(f"Error processing LLM response: {e}")
                raise
                
            # Create schedule
            schedule_id = await self.schedule_manager.initialize_schedule(
                tool_operation_id=tool_operation_id,
                schedule_info={
                    "schedule_type": params.get("schedule_type"),
                    "schedule_time": params.get("schedule_time"),
                    "total_items": params["item_count"],
                    **{k: v for k, v in params.items() if k not in ["topic", "item_count"]}
                },
                content_type=ContentType.TWEET.value,
                session_id=self.deps.session_id
            )
            
            # Update operation with command info only
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,
                input_data={
                    "command_info": {
                        "topic": params["topic"],
                        "item_count": params["item_count"],
                        "schedule_type": params.get("schedule_type"),
                        "schedule_time": params.get("schedule_time")
                    },
                    "schedule_id": schedule_id
                }
            )
            
            return {
                "schedule_id": schedule_id,
                "tool_operation_id": tool_operation_id,
                "topic": params["topic"],
                "item_count": params["item_count"]
            }

        except Exception as e:
            logger.error(f"Error in Twitter command analysis: {e}", exc_info=True)
            raise

    async def _generate_tweets(self, topic: str, count: int, schedule_id: str = None, tool_operation_id: str = None) -> Dict:
        """Generate tweet content and save as tool items"""
        try:
            logger.info(f"Starting tweet generation: {count} tweets about {topic}")
            
            # Get parent operation to inherit state/status
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
                
            # Verify operation is in correct state
            if operation["state"] != ToolOperationState.COLLECTING.value:
                raise ValueError(f"Operation in invalid state: {operation['state']}")
            
            # Add check for regeneration
            is_regenerating = operation.get("metadata", {}).get("approval_state") == ApprovalState.REGENERATING.value
            logger.info(f"Generating tweets in {'regeneration' if is_regenerating else 'initial'} mode")
            
            # Generate tweets using LLM
            prompt = f"""You are a professional social media manager. Generate {count} engaging tweets about {topic}.

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

            logger.info(f"Sending generation prompt to LLM: {messages}")
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.7,
                    "max_tokens": 1000
                }
            )
            
            logger.info(f"Raw LLM response: {response}")
            
            # Strip markdown code blocks if present
            response = response.strip()
            if response.startswith('```') and response.endswith('```'):
                # Remove the first line (```json) and the last line (```)
                response = '\n'.join(response.split('\n')[1:-1])
            
            generated_items = json.loads(response)
            logger.info(f"Parsed generated items: {generated_items}")
            
            # Transform and save items with proper state inheritance
            saved_items = []
            current_pending_items = operation.get("output_data", {}).get("pending_items", [])
            
            for item in generated_items.get('items', []):
                tool_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": tool_operation_id,
                    "schedule_id": schedule_id,
                    "content_type": ContentType.TWEET.value,
                    "state": operation["state"],  # Inherit COLLECTING state
                    "status": OperationStatus.PENDING.value,  # Individual item status
                    "content": {
                        "raw_content": item["content"],
                        "formatted_content": item["content"],
                        "version": "1.0"
                    },
                    "metadata": {
                        **item.get("metadata", {}),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "parent_operation_state": operation["state"],
                        "state_history": [{
                            "state": operation["state"],
                            "status": OperationStatus.PENDING.value,
                            "timestamp": datetime.now(UTC).isoformat()
                        }]
                    }
                }
                
                # Save item
                result = await self.db.tool_items.insert_one(tool_item)
                item_id = str(result.inserted_id)
                
                # Add to pending items list
                current_pending_items.append(item_id)
                
                # Update parent operation with new pending item
                await self.tool_state_manager.update_operation(
                    session_id=self.deps.session_id,
                    tool_operation_id=tool_operation_id,
                    content_updates={
                        "pending_items": current_pending_items
                    },
                    metadata={
                        "item_states": {
                            item_id: {
                                "state": operation["state"],
                                "status": OperationStatus.PENDING.value
                            }
                        }
                    }
                )
                
                saved_item = {**tool_item, "_id": item_id}
                saved_items.append(saved_item)
                logger.info(f"Saved tool item {item_id} with state {operation['state']}")

            logger.info(f"Generated and saved {len(saved_items)} tweet items")
            
            if is_regenerating:
                return {
                    "items": saved_items,
                    "schedule_id": schedule_id,
                    "tool_operation_id": tool_operation_id,
                    "regeneration_needed": True,
                    "regenerate_count": len(saved_items)
                }

            return {
                "items": saved_items,
                "schedule_id": schedule_id,
                "tool_operation_id": tool_operation_id
            }

        except Exception as e:
            logger.error(f"Error generating tweets: {e}", exc_info=True)
            raise

    async def execute_scheduled_operation(self, operation: Dict) -> Dict:
        """Execute a scheduled tweet operation"""
        try:
            content = operation.get('content', {}).get('formatted_content')
            if not content:
                raise ValueError("No content found for scheduled tweet")

            result = await self.twitter_client.send_tweet(
                content=content,
                params={
                    'account_id': operation.get('metadata', {}).get('account_id', 'default'),
                    'media_files': operation.get('metadata', {}).get('media_files', []),
                    'poll_options': operation.get('metadata', {}).get('poll_options', [])
                }
            )
            
            return {
                'success': result.get('success', False),
                'result': result,
                'tweet_id': result.get('id')
            }
            
        except Exception as e:
            logger.error(f"Error executing scheduled tweet: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    async def _get_db(self):
        """Get database instance"""
        return MongoManager.get_db()

    async def _handle_error(
        self,
        content_id: str,
        session_id: str,
        analysis: Dict,
        metadata: Dict = None
    ) -> Dict:
        """Handle error in approval flow"""
        try:
            error_message = analysis.get('feedback', 'An error occurred in the approval process')
            logger.error(f"Approval error: {error_message}")
            
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                state=ToolOperationState.ERROR,
                step="error",
                content_updates={},
                metadata={
                    **(metadata or {}),
                    "error": error_message,
                    "error_timestamp": datetime.now(UTC).isoformat(),
                    "error_type": "approval_error"
                }
            )
            
            return {
                "status": "error",
                "response": f"Error in approval process: {error_message}",
                "requires_tts": True
            }
            
        except Exception as e:
            logger.error(f"Error handling approval error: {e}")
            return self.approval_manager._create_error_response(str(e))

    async def _regenerate_rejected_tweets(
        self,
        tool_operation_id: str,
        regenerate_count: int,
        analysis: Dict,
        **kwargs
    ) -> Dict:
        """Handle tweet regeneration after partial approval"""
        try:
            # Get operation for topic
            operation = await self.tool_state_manager.get_operation_by_id(tool_operation_id)
            if not operation:
                raise ValueError(f"No operation found for ID {tool_operation_id}")

            # Get topic from command_info
            topic = operation.get("input_data", {}).get("command_info", {}).get("topic")
            if not topic:
                raise ValueError("Could not find topic for regeneration")

            logger.info(f"Regenerating {regenerate_count} tweets about topic: {topic}")

            # Generate new tweet content - _generate_tweets handles state management
            return await self._generate_tweets(
                topic=topic, 
                count=regenerate_count, 
                schedule_id=operation.get("input_data", {}).get("schedule_id"), 
                tool_operation_id=tool_operation_id
            )

        except Exception as e:
            logger.error(f"Error regenerating tweets: {e}")
            return self.approval_manager._create_error_response(str(e))
