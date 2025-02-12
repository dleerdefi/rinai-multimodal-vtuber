from datetime import datetime, UTC, timedelta
import logging
from typing import Dict, List, Optional, Any
import json

from src.tools.base import (
    BaseTool,
    AgentResult,
    AgentDependencies,
    TweetContent,
    CommandAnalysis,  # Used in _analyze_twitter_command return type
    TweetGenerationResponse,
    TweetApprovalAnalysis
)
from src.managers.tool_state_manager import (
    ToolStateManager, 
    ToolOperationState, 
    TweetStatus
)
from src.services.llm_service import LLMService, ModelType
from src.db.mongo_manager import MongoManager
from src.db.db_schema import TweetStatus as DBTweetStatus, Tweet, TweetSchedule

from src.utils.json_parser import parse_strict_json  # Add this import

logger = logging.getLogger(__name__)

class TweetTool(BaseTool):
    name = "tweet_scheduler"
    description = "Tweet scheduling and management tool"
    version = "1.0.0"

    def __init__(self, 
                 deps: Optional[AgentDependencies] = None, 
                 tool_state_manager: Optional[ToolStateManager] = None,
                 llm_service: Optional[LLMService] = None):
        """Initialize tweet tool with dependencies and services"""
        super().__init__()
        self.deps = deps
        self.db = MongoManager.get_db()
        # Use provided tool_state_manager or create new one
        self.tool_state_manager = tool_state_manager or ToolStateManager(db=self.db)
        # Use provided llm_service or create new one
        self.llm_service = llm_service or LLMService()

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by tweet tool"""
        return isinstance(input_data, str)  # Basic type check only

    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Main execution method"""
        try:
            # Analyze the command first
            analysis = await self._analyze_twitter_command(input_data)
            
            if not analysis:
                return {
                    "status": "error",
                    "error": "Failed to analyze Twitter command",
                    "timestamp": datetime.utcnow().isoformat()
                }
                
            return {
                "status": "success",
                "data": analysis,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error in tweet tool execution: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _analyze_twitter_command(self, command: str, session_id: Optional[str] = None) -> CommandAnalysis:
        """Specialized analysis for Twitter commands"""
        try:
            # Use provided session_id or get from deps as fallback
            session_id = session_id or (self.deps.conversation_id if self.deps else None)
            
            # Start a tool operation for Twitter
            operation = await self.tool_state_manager.start_operation(
                session_id=session_id,
                operation_type="twitter",
                initial_data={
                    "command": command,
                    "status": TweetStatus.PENDING.value
                }
            )
            
            prompt = f"""You are a Twitter action analyzer. Determine the specific Twitter action needed.

Command: "{command}"

Available Twitter actions: 
1. send_tweet: Post a new tweet immediately
   Parameters: message, account_id (optional)

2. schedule_tweets: Schedule one or more tweets for later
   Parameters: 
   - tweet_count: number of tweets to schedule
   - topic: what to tweet about
   - schedule_type: "one_time"
   - schedule_time: when to post (default: spread over next 24 hours)
   - approval_required: true

Instructions:
- Return ONLY valid JSON matching the example format
- Extract count, topic, and timing information from command
- If no specific time mentioned, default to spreading tweets over next 24 hours
- Include schedule_time in parameters

Example response format:
{{
    "tools_needed": [{{
        "tool_name": "twitter",
        "action": "schedule_tweets",
        "parameters": {{
            "tweet_count": 5,
            "topic": "artificial intelligence",
            "schedule_type": "one_time",
            "schedule_time": "spread_24h",
            "approval_required": true
        }},
        "priority": 1
    }}],
    "reasoning": "User requested scheduling multiple tweets about AI"
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

            # Get LLM response
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 200
                }
            )

            try:
                # Extract JSON if response contains extra text
                response = response.strip()
                start_idx = response.find('{')
                end_idx = response.rfind('}') + 1
                if start_idx != -1 and end_idx != 0:
                    json_str = response[start_idx:end_idx]
                    logger.debug(f"Extracted JSON string: {json_str}")
                    data = json.loads(json_str)
                    return CommandAnalysis(**data)
                else:
                    logger.error(f"No JSON found in response: {response}")
                    raise ValueError("No valid JSON found in response")
                    
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Failed to parse response: {response}")
                logger.error(f"Parse error: {str(e)}")
                raise

        except Exception as e:
            logger.error(f"Error in Twitter command analysis: {e}")
            if session_id:
                await self.tool_state_manager.end_operation(session_id, success=False)
            raise

    async def _generate_tweet_series(self, topic: str, count: int, tone: str = "professional", 
                                   original_request: str = None, session_id: Optional[str] = None) -> Dict:
        """Generate one or more tweets about a topic"""
        try:
            # Get session_id with proper fallback chain
            current_session_id = (
                session_id or 
                (self.deps.conversation_id if self.deps else None)
            )
            
            if not current_session_id:
                raise ValueError("No session_id available")

            # Create schedule first with proper positional arguments
            schedule_id = await self.db.create_tweet_schedule(
                session_id=current_session_id,
                topic=topic,
                total_tweets=count,
                schedule_info={
                    "topic": topic,
                    "total_tweets": count,
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "start_time": datetime.now(UTC),
                    "interval_minutes": 60,
                    "schedule_type": "one_time",
                    "metadata": {
                        "tone": tone,
                        "original_request": original_request
                    }
                }
            )

            # Generate tweets using existing logic
            # TODO: Add perplexity search call to enrich contents for tweet generation.
            response = await self.llm_service.get_response(
                prompt=[
                    {
                        "role": "system",
                        "content": "You are an expert tweet creator. Create tweet(s) about the topic provided. Return ONLY valid JSON."
                    },
                    {
                        "role": "user",
                        "content": f"""Generate {'a single' if count == 1 else str(count)} engaging tweet{'s' if count > 1 else ''} about {topic}.
                        
Requirements:
- {'Each tweet must be' if count > 1 else 'Must be'} under 280 characters
- Maintain a {tone} tone
- Be engaging and natural
- Return as JSON: {{"tweets": [{{"content": "tweet text"}}]}}"""
                    }
                ],
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.7,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"}
                }
            )

            # Parse and validate tweets using parse_strict_json
            tweet_data = parse_strict_json(response, TweetGenerationResponse)
            if not tweet_data:
                logger.error("Failed to parse tweet generation response")
                raise ValueError("Failed to generate valid tweets")

            validated_tweets = []
            for tweet in tweet_data.tweets:
                if len(tweet.content) <= 280:
                    tweet_data = {
                        "content": tweet.content,
                        "metadata": {
                            "estimated_engagement": "medium",
                            "generated_at": datetime.utcnow().isoformat()
                        }
                    }
                    validated_tweets.append(tweet_data)

            if not validated_tweets:
                raise ValueError("No valid tweets generated")

            # Store tweets with schedule reference
            stored_tweet_ids = []
            for tweet in validated_tweets:
                tweet_id = await self.db.create_tweet(
                    content=tweet["content"],
                    schedule_id=schedule_id,
                    session_id=current_session_id
                )
                stored_tweet_ids.append(tweet_id)

            # Update schedule with pending tweet IDs
            await self.db.update_tweet_schedule(
                schedule_id=schedule_id,
                pending_tweet_ids=stored_tweet_ids,
                status="collecting_approval"
            )

            return {
                "schedule_id": schedule_id,
                "tweets": validated_tweets,
                "stored_tweet_ids": stored_tweet_ids
            }

        except Exception as e:
            logger.error(f"Error generating tweet series: {e}")
            raise

    async def _store_approved_tweets(self, tweets: List[Dict], schedule_info: Dict) -> str:
        """Store approved tweets using RinDB schema"""
        db = MongoManager.get_db()
        
        # Create or update tweet schedule
        schedule_id = await db.create_tweet_schedule(
            session_id=schedule_info.get('session_id'),
            topic=schedule_info.get('topic', 'general'),
            total_tweets=len(tweets),
            schedule_info=schedule_info
        )
        
        # Store individual tweets
        for tweet in tweets:
            await db.create_tweet(
                content=tweet['content'],
                schedule_id=schedule_id,
                session_id=schedule_info.get('session_id'),
                scheduled_time=tweet.get('scheduled_time')
            )
            
        return schedule_id

    async def _execute_tweet(self, tweet_data: Dict) -> Dict:
        """Execute a single tweet using TwitterAgentClient"""
        try:
            result = await self.twitter_client.send_tweet(**tweet_data["twitter_api_params"])
            return {
                "status": "success",
                "tweet_id": result.get("id"),
                "timestamp": datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error posting tweet: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _handle_tweet_approval_flow(self, tweets: List[Dict], session_id: str, approved_tweets: List[Dict] = None) -> Dict:
        """Handle tweet approval flow without analysis step"""
        try:
            db = MongoManager.get_db()
            
            # Update operation state to collecting approvals
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                state=ToolOperationState.COLLECTING,
                step="awaiting_approval"
            )
            
            # Get existing schedule
            schedule = await db.get_session_tweet_schedule(session_id)
            if not schedule:
                return {
                    "status": "error",
                    "response": "Could not find active tweet schedule.",
                    "requires_tts": True
                }
            
            # Store pending tweets directly
            stored_tweet_ids = []
            for tweet in tweets:
                tweet_id = await db.create_tweet(
                    content=tweet["content"],
                    schedule_id=str(schedule["_id"]),
                    session_id=session_id
                )
                stored_tweet_ids.append(tweet_id)

            # Update schedule with pending tweets
            await db.update_tweet_schedule(
                schedule_id=str(schedule["_id"]),
                pending_tweet_ids=stored_tweet_ids,
                status="collecting_approval"
            )

            # Format tweets for presentation
            tweets_presentation = "\n\n".join([
                f"Tweet {i+1}:\n{tweet['content']}" 
                for i, tweet in enumerate(tweets)
            ])

            # Format response for user
            remaining = schedule['total_tweets_requested'] - len(approved_tweets or [])
            tts_response = (
                f"I've prepared {len(tweets)} new tweet{'s' if len(tweets) > 1 else ''}. Here they are:\n\n"
                f"{tweets_presentation}\n\n"
                f"Would you like to:\n"
                "Approve all tweets\n"
                "Approve specific tweets\n"
                "Request changes\n"
                f"We need {remaining} more tweet(s) to complete the schedule."
            )

            return {
                "status": "awaiting_approval",
                "response": tts_response,
                "requires_tts": True,
                "data": {
                    "tweets": tweets,
                    "remaining": remaining
                }
            }

        except Exception as e:
            logger.error(f"Error in tweet approval flow: {e}")
            await self.tool_state_manager.update_operation(
                session_id=session_id,
                state=ToolOperationState.ERROR,
                step="approval_flow_error"
            )
            return {
                "status": "error",
                "response": "I encountered an error processing the tweets. Would you like to try again?",
                "requires_tts": True
            }

    def _format_tweets_for_presentation(self, tweets: List[Dict]) -> str:
        """Format tweets for TTS-friendly presentation"""
        formatted = []
        for i, tweet in enumerate(tweets, 1):
            formatted.append(f"Tweet {i}:\n{tweet['content']}\n")
        return "\n".join(formatted)

    async def _process_tweet_approval_response(self, message: str, session_id: str) -> Dict:
        """Process user's response to tweet approval"""
        try:
            # Get operation state first
            operation_state = await self.tool_state_manager.get_operation_state(session_id)
            logger.debug(f"Current operation state: {operation_state}")
            
            # If tweets are already approved or scheduled, don't process any more commands
            if operation_state and operation_state.get("status") in ["approved", "scheduled"]:
                return {
                    "status": "success",
                    "response": "Your tweets are already scheduled and being processed. You can safely exit.",
                    "requires_tts": True
                }
            
            # More lenient state validation - accept any collecting state
            if not operation_state or operation_state.get('state') != 'collecting':
                logger.warning(f"Invalid state for tweet approval: {operation_state}")
                return {
                    "status": "error",
                    "response": "I've lost track of our tweet approval process. Would you like to start over?",
                    "requires_tts": True
                }

            # Check for exit commands first
            exit_keywords = ["stop", "cancel", "exit", "quit", "end", "terminate"]
            if any(keyword in message.lower() for keyword in exit_keywords):
                logger.info(f"Exit command detected: {message}")
                await self.tool_state_manager.end_operation(
                    session_id=session_id,
                    success=False,
                    reason="User requested to stop"
                )
                return {
                    "status": "cancelled",
                    "response": "I've stopped the tweet process. Let me know if you need anything else!",
                    "requires_tts": True
                }

            # Get active schedule and pending tweets
            db = MongoManager.get_db()
            schedule = await db.get_session_tweet_schedule(session_id)
            if not schedule:
                logger.error("No active schedule found")
                return {
                    "status": "error",
                    "response": "I couldn't find the tweets we were discussing.",
                    "requires_tts": True
                }

            # Get ONLY the most recent pending tweets
            pending_tweets = await db.get_tweets_by_schedule(str(schedule["_id"]))
            pending_tweets = [t for t in pending_tweets if t["status"] == TweetStatus.PENDING.value]
            logger.info(f"Found {len(pending_tweets)} pending tweets")

            # Analyze user response with LLM
            analysis = await self._analyze_tweets_and_response(
                tweets=pending_tweets,
                user_response=message,
                metadata={"topic": schedule.get("topic", "general")}
            )
            logger.info(f"Analysis result: {analysis}")

            try:
                # Access Pydantic model attributes with dot notation
                if analysis.action == "full_approval":
                    # Update all pending tweets to approved
                    for tweet in pending_tweets:
                        await db.update_tweet_status(
                            tweet_id=str(tweet["_id"]),
                            status=TweetStatus.APPROVED.value
                        )
                    
                    # Update schedule status
                    await db.update_tweet_schedule(
                        schedule_id=str(schedule["_id"]),
                        status="ready_to_schedule"
                    )
                    
                    # Activate the schedule
                    schedule_activated = await self._activate_tweet_schedule(
                        str(schedule["_id"]), 
                        schedule["schedule_info"]
                    )
                    
                    if schedule_activated:
                        return {
                            "status": "completed",
                            "response": analysis.feedback,
                            "requires_tts": True
                        }

                elif analysis.action in ["partial_approval", "partial_regenerate"]:
                    # Update operation state to track partial approval
                    await self.tool_state_manager.update_operation(
                        session_id=session_id,
                        state=ToolOperationState.COLLECTING,
                        step="partial_approval_in_progress",
                        data={
                            "schedule_id": str(schedule["_id"]),
                            "approved_indices": analysis.approved_indices,
                            "pending_indices": analysis.regenerate_indices,
                            "total_needed": schedule["total_tweets_requested"]
                        }
                    )

                    # Process approved tweets first
                    for idx in analysis.approved_indices:
                        if 0 <= idx - 1 < len(pending_tweets):
                            await db.update_tweet_status(
                                tweet_id=str(pending_tweets[idx - 1]["_id"]),
                                status=TweetStatus.APPROVED.value
                            )
                    
                    # Mark and regenerate rejected tweets immediately
                    rejected_count = len(analysis.regenerate_indices)
                    if rejected_count > 0:
                        # Mark tweets as rejected
                        for idx in analysis.regenerate_indices:
                            if 0 <= idx - 1 < len(pending_tweets):
                                await db.update_tweet_status(
                                    tweet_id=str(pending_tweets[idx - 1]["_id"]),
                                    status=TweetStatus.REJECTED.value
                                )
                        
                        # Get metadata from both possible locations in schedule
                        schedule_info = schedule.get("schedule_info", {})
                        schedule_metadata = schedule.get("metadata", {}) or {}  # Ensure it's never None

                        # Get nested metadata from schedule_info safely
                        schedule_info_metadata = schedule_info.get("metadata", {}) or {}

                        # Combine them with schedule_info taking precedence
                        tone = (
                            schedule_info_metadata.get("tone") or 
                            schedule_metadata.get("tone") or 
                            "professional"
                        )
                        original_request = (
                            schedule_info_metadata.get("original_request") or 
                            schedule_metadata.get("original_request")
                        )
                        
                        # Generate new tweets with proper metadata handling
                        new_tweets = await self._generate_tweet_series(
                            topic=schedule["topic"],
                            count=rejected_count,
                            tone=tone,
                            original_request=original_request,
                            session_id=session_id
                        )
                        
                        # Store new tweets with metadata
                        stored_tweet_ids = []
                        for tweet in new_tweets["tweets"]:
                            # Create tweet with basic parameters
                            tweet_id = await db.create_tweet(
                                content=tweet["content"],
                                schedule_id=str(schedule["_id"]),
                                session_id=session_id
                            )
                            
                            # Update the tweet's metadata separately
                            await db.update_tweet_status(
                                tweet_id=tweet_id,
                                status=TweetStatus.PENDING.value,
                                metadata={
                                    "original_request": original_request,
                                    "tone": tone,
                                    "generated_at": datetime.now(UTC).isoformat()
                                }
                            )
                            stored_tweet_ids.append(tweet_id)
                        
                        return {
                            "status": "partial_regenerated",
                            "response": f"{analysis.feedback}\nI've kept the approved tweets and generated new ones to replace the others. Here are the new tweets:\n{self._format_tweets_for_presentation(new_tweets['tweets'])}",
                            "requires_tts": True,
                            "data": {
                                "new_tweets": new_tweets,
                                "stored_tweet_ids": stored_tweet_ids,
                                "regenerate_count": rejected_count
                            }
                        }

                elif analysis.action == "regenerate_all":
                    logger.info("Regenerating all tweets")
                    # Mark existing tweets as rejected
                    for tweet in pending_tweets:
                        await db.update_tweet_status(
                            tweet_id=str(tweet["_id"]),
                            status=TweetStatus.REJECTED.value
                        )
                    
                    # Get metadata from both possible locations in schedule
                    schedule_info = schedule.get("schedule_info", {})
                    schedule_metadata = schedule.get("metadata", {}) or {}  # Ensure it's never None

                    # Get nested metadata from schedule_info safely
                    schedule_info_metadata = schedule_info.get("metadata", {}) or {}

                    # Combine them with schedule_info taking precedence
                    tone = (
                        schedule_info_metadata.get("tone") or 
                        schedule_metadata.get("tone") or 
                        "professional"
                    )
                    original_request = (
                        schedule_info_metadata.get("original_request") or 
                        schedule_metadata.get("original_request")
                    )
                    
                    # Generate new tweets with proper metadata handling
                    new_tweets = await self._generate_tweet_series(
                        topic=schedule["topic"],
                        count=schedule["total_tweets_requested"],
                        tone=tone,
                        original_request=original_request,
                        session_id=session_id
                    )
                    
                    # Store new tweets with metadata
                    stored_tweet_ids = []
                    for tweet in new_tweets["tweets"]:
                        tweet_id = await db.create_tweet(
                            content=tweet["content"],
                            schedule_id=str(schedule["_id"]),
                            session_id=session_id,
                            status=TweetStatus.PENDING.value,
                            metadata={
                                "original_request": original_request,
                                "tone": tone,
                                "generated_at": datetime.now(UTC).isoformat()
                            }
                        )
                        stored_tweet_ids.append(tweet_id)
                    
                    # Update schedule
                    await db.update_tweet_schedule(
                        schedule_id=str(schedule["_id"]),
                        pending_tweet_ids=stored_tweet_ids,
                        status="collecting_approval"
                    )
                    
                    return {
                        "status": "regenerated",
                        "response": f"I've generated new tweets for you. Here they are:\n{self._format_tweets_for_presentation(new_tweets['tweets'])}",
                        "requires_tts": True,
                        "data": {"tweets": new_tweets}
                    }

                # Check remaining tweets needed
                approved_tweets = await db.get_tweets_by_schedule(str(schedule["_id"]))
                approved_count = sum(1 for t in approved_tweets if t["status"] == TweetStatus.APPROVED.value)
                remaining = schedule["total_tweets_requested"] - approved_count

                if remaining > 0:
                    return {
                        "status": "in_progress",
                        "response": f"{analysis.feedback}\nWe still need {remaining} more tweets. Would you like me to generate them now?",
                        "requires_tts": True
                    }

                return {
                    "status": "awaiting_input",
                    "response": f"{analysis.feedback}\nWhat would you like me to do with these tweets?",
                    "requires_tts": True
                }

            except Exception as e:
                logger.error(f"Error in approval action processing: {e}", exc_info=True)
                return {
                    "status": "error",
                    "response": "I had trouble processing your approval. Would you like to try again?",
                    "requires_tts": True
                }

        except Exception as e:
            logger.error(f"Error processing approval: {e}", exc_info=True)
            # ... outer exception handling ...

    async def _activate_tweet_schedule(self, schedule_id: str, schedule_info: Dict) -> bool:
        """Activate a tweet schedule after approval"""
        try:
            # Get all approved tweets for this schedule
            tweets = await self.db.get_tweets_by_schedule(schedule_id)
            approved_tweets = [t for t in tweets if t["status"] == TweetStatus.APPROVED.value]
            
            # Get or create start_time
            start_time = schedule_info.get("start_time")
            if not start_time:
                start_time = datetime.now(UTC)
                logger.info(f"No start_time found, using current time: {start_time}")
            elif isinstance(start_time, str):
                start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                logger.info(f"Parsed start_time from string: {start_time}")
            
            interval_minutes = schedule_info.get("interval_minutes", 2)
            interval = timedelta(minutes=interval_minutes)
            
            logger.info(f"Scheduling {len(approved_tweets)} tweets starting at {start_time} with {interval_minutes} minute intervals")
            
            # Update each tweet with its schedule time
            for i, tweet in enumerate(approved_tweets):
                scheduled_time = start_time + (interval * i)
                # Update tweet status and store scheduled_time in metadata
                await self.db.update_tweet_status(
                    tweet_id=str(tweet["_id"]),
                    status=TweetStatus.SCHEDULED.value,
                    metadata={
                        "scheduled_time": scheduled_time.isoformat(),
                        "schedule_index": i
                    }
                )
                logger.info(f"Scheduled tweet {tweet['_id']} for {scheduled_time}")
            
            # Update schedule status with proper datetime handling
            await self.db.update_tweet_schedule(
                schedule_id=schedule_id,
                status="scheduled",
                schedule_info={
                    **schedule_info,
                    "start_time": start_time.isoformat(),
                    "interval_minutes": interval_minutes,
                    "last_updated": datetime.now(UTC).isoformat()
                }
            )
            
            logger.info(f"Successfully activated schedule {schedule_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error activating tweet schedule: {e}", exc_info=True)
            return False

    async def _analyze_tweets_and_response(self, tweets: List[Dict], user_response: str, metadata: Dict = None) -> Dict:
        """Analyze user's response to tweets"""
        try:
            presentation = self._format_tweets_for_presentation(tweets)
            
            prompt = [
                {
                    "role": "system",
                    "content": "Analyze user instructions on how to proceed with the proposed draft tweet(s) and return structured JSON."
                },
                {
                    "role": "user",
                    "content": f"""Context: Previous tweets presented:
{presentation}

User response: "{user_response}"

There are {len(tweets)} tweets to analyze. Return ONLY valid JSON in this exact format:
{{
    "action": "full_approval" | "partial_approval" | "regenerate_all" | "partial_regenerate",
    "approved_indices": [list of approved tweet numbers from 1 to {len(tweets)}],
    "regenerate_indices": [list of tweet numbers to regenerate from 1 to {len(tweets)}],
    "feedback": "explanation in Rin's voice"
}}"""
                }
            ]

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GPT4o,
                override_config={
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"}
                }
            )

            # Use the new parse_approval_response method
            return await self._parse_approval_response(response)

        except Exception as e:
            logger.error(f"Error in tweet response analysis: {e}")
            raise

    async def _parse_approval_response(self, response: str) -> TweetApprovalAnalysis:
        """Parse the approval response from the LLM"""
        try:
            # Use existing parse_strict_json from json_parser with TweetApprovalAnalysis model
            data = parse_strict_json(response, TweetApprovalAnalysis)
            
            if not data:
                logger.warning("Failed to parse approval response, using default regenerate")
                return TweetApprovalAnalysis(
                    action='regenerate_all',
                    approved_indices=[],
                    regenerate_indices=[1, 2],
                    feedback="I'll generate new tweets for you."
                )
                
            return data

        except Exception as e:
            logger.error(f"Failed to parse approval response: {e}")
            return TweetApprovalAnalysis(
                action='regenerate_all',
                approved_indices=[],
                regenerate_indices=[1, 2],
                feedback="I'll generate new tweets for you."
            )

    async def _get_db(self):
        """Get database instance"""
        return MongoManager.get_db()


    async def _validate_command_analysis(self, analysis_result: Dict, current_state: Dict) -> Dict:
        """Validate if the command analysis aligns with the current system state"""
        try:
            # Extract current state information
            actual_tweet_count = len(current_state.get("pending_tweets", []))
            original_topic = current_state.get("topic")
            original_request = current_state.get("original_request")

            # Extract analyzed parameters
            analyzed_params = analysis_result.get("tools_needed", [{}])[0].get("parameters", {})
            
            # Validate key parameters
            mismatches = []
            if analyzed_params.get("tweet_count") != actual_tweet_count:
                mismatches.append({
                    "field": "tweet_count",
                    "expected": actual_tweet_count,
                    "received": analyzed_params.get("tweet_count"),
                    "severity": "high"
                })
            
            if analyzed_params.get("topic") != original_topic:
                mismatches.append({
                    "field": "topic",
                    "expected": original_topic,
                    "received": analyzed_params.get("topic"),
                    "severity": "high"
                })

            # If mismatches found, correct the analysis
            if mismatches:
                logger.warning(f"Command analysis mismatches found: {mismatches}")
                # Create corrected version of the command
                corrected_command = {
                    "tools_needed": [{
                        "tool_name": "twitter",
                        "action": "schedule_tweets",
                        "parameters": {
                            "tweet_count": actual_tweet_count,
                            "topic": original_topic,
                            "schedule_type": analyzed_params.get("schedule_type", "one_time"),
                            "schedule_time": analyzed_params.get("schedule_time", "now"),
                            "approval_required": True
                        },
                        "priority": 1
                    }],
                    "reasoning": f"Corrected analysis for scheduling {actual_tweet_count} tweets about {original_topic}"
                }
                return {
                    "is_valid": False,
                    "corrected_analysis": corrected_command,
                    "mismatches": mismatches
                }
            
            return {
                "is_valid": True,
                "original_analysis": analysis_result
            }

        except Exception as e:
            logger.error(f"Error validating command analysis: {e}")
            return {
                "is_valid": False,
                "error": str(e),
                "severity": "high"
            }