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
)
from src.db.enums import OperationStatus, ToolOperationState, ScheduleState, ContentType, ToolType
from src.utils.json_parser import parse_strict_json
from src.managers.approval_manager import ApprovalManager, ApprovalAction, ApprovalState
from src.managers.schedule_manager import ScheduleManager

logger = logging.getLogger(__name__)

class TwitterTool(BaseTool):
    """Tool for posting and managing tweets"""
    
    # Static tool configuration
    name = "twitter"
    description = "Post and schedule tweets"
    version = "1.0"
    
    # Tool registry configuration
    registry = ToolRegistry(
        content_type=ContentType.TWEET,
        tool_type=ToolType.TWITTER,
        requires_approval=True,
        requires_scheduling=True,
        required_clients=["twitter_client", "perplexity_client"],
        required_managers=["tool_state_manager", "approval_manager", "schedule_manager"]
    )

    def __init__(self, deps: Optional[AgentDependencies] = None):
        """Initialize tweet tool with dependencies
        
        Args:
            deps: Optional AgentDependencies instance. If not provided, 
                 dependencies will be injected by the orchestrator.
        """
        super().__init__()
        self.deps = deps or AgentDependencies()
        
        # Services will be injected by orchestrator based on registry requirements
        self.tool_state_manager = None
        self.llm_service = None
        self.approval_manager = None
        self.schedule_manager = None
        self.perplexity_client = None
        self.db = None

    def inject_dependencies(self, **services):
        """Inject required services - called by orchestrator during registration"""
        self.tool_state_manager = services.get("tool_state_manager")
        self.llm_service = services.get("llm_service")
        self.approval_manager = services.get("approval_manager")
        self.schedule_manager = services.get("schedule_manager")
        self.perplexity_client = services.get("perplexity_client")
        self.db = self.tool_state_manager.db if self.tool_state_manager else None

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by tweet tool"""
        return isinstance(input_data, str)  # Basic type check only

    async def run(self, input_data: str) -> Dict:
        """Run the tweet tool - handles only initial content generation"""
        try:
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            
            if not operation or operation.get('state') == ToolOperationState.COMPLETED.value:
                # Initial tweet generation flow
                command_info = await self._analyze_command(input_data)
                generation_result = await self._generate_content(
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
                # Let orchestrator handle ongoing operations
                raise ValueError("Operation already in progress - should be handled by orchestrator")

        except Exception as e:
            logger.error(f"Error in tweet tool: {e}", exc_info=True)
            return self.approval_manager.analyzer.create_error_response(str(e))

    async def _analyze_command(self, command: str) -> Dict:
        """Analyze command and setup initial schedule"""
        try:
            logger.info(f"Starting command analysis for: {command}")
            
            # Get the existing operation that was created by orchestrator
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
                
            tool_operation_id = str(operation['_id'])
            
            # Update the operation with registry settings
            await self.tool_state_manager.update_operation(
                session_id=self.deps.session_id,
                tool_operation_id=tool_operation_id,  # Required parameter from existing operation
                input_data={
                    "command": command,
                    "content_type": self.registry.content_type.value,
                    "tool_registry": {
                        "requires_approval": self.registry.requires_approval,
                        "requires_scheduling": self.registry.requires_scheduling,
                        "content_type": self.registry.content_type.value
                    }
                }
            )
            
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
   - schedule_required: true

Instructions:
- Return ONLY valid JSON matching the example format
- Extract count, topic, and ALL timing information from command
- For spread_24h, calculate appropriate interval based on tweet count
- For specific times, provide start_time in ISO format
- Include ALL scheduling parameters
- Follow the exact schema provided
- Include NO additional text or markdown

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
            "approval_required": true,
            "schedule_required": true
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
                content_type=self.registry.content_type.value,
                session_id=self.deps.session_id
            )
            
            # Update operation with command info and schedule info
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
                },
                metadata={
                    "schedule_state": ScheduleState.PENDING.value,
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

    async def _generate_content(
        self, 
        topic: str, 
        count: int, 
        schedule_id: str = None, 
        tool_operation_id: str = None,
        revision_instructions: str = None
    ) -> Dict:
        """Generate tweet content and save as tool items"""
        try:
            logger.info(f"Starting tweet generation: {count} tweets about {topic}")
            
            # 1. Get operation state
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")

            # 2. Check for regeneration FIRST
            is_regenerating = operation.get("metadata", {}).get("approval_state") == ApprovalState.REGENERATING.value
            logger.info(f"Generating tweets in {'regeneration' if is_regenerating else 'initial'} mode")

            # 3. State validation based on generation type
            if not is_regenerating and operation["state"] != ToolOperationState.COLLECTING.value:
                raise ValueError(f"Operation in invalid state for initial generation: {operation['state']}")
            elif is_regenerating and operation["state"] != ToolOperationState.APPROVING.value:
                raise ValueError(f"Operation in invalid state for regeneration: {operation['state']}")

            # 4. Get Perplexity context FIRST - needed for both flows
            perplexity_prompt = f"""
Research the latest information about {topic} to create high-engagement tweets.
Analyze and synthesize:

1. BREAKING NEWS (last 12 hours): What's the most recent development that would surprise or inform our audience?

2. DATA INSIGHTS: What specific numbers, percentages, or statistics demonstrate significant trends or changes? Include exact figures with sources.

3. EXPERT ANALYSIS: What are recognized authorities saying that challenges conventional wisdom or offers unique perspectives? Include direct quotes with attribution.

4. AUDIENCE DISCUSSIONS: What specific questions or debates are generating the most engagement in online communities? What are people confused about or wanting to know?

5. PREDICTION ANGLES: What forecasts or future implications are being discussed that our audience should know about?

6. CONTRARIAN VIEWPOINTS: What counter-intuitive perspectives exist that might provoke thought or discussion?

7. TIME-SENSITIVE OPPORTUNITIES: What upcoming events, deadlines, or releases should our audience be aware of?

Format your response with clearly labeled sections and bullet points. For each point, include:
- The specific insight
- Why it matters (relevance/impact)
- Source attribution where applicable
- Engagement potential (why people would care)

Prioritize information that is:
- Timely (preferably within last 24 hours)
- Specific rather than general
- Surprising or challenging to common assumptions
- Actionable or decision-relevant
- Emotionally resonant
"""

            context_info = ""
            if self.perplexity_client:
                try:
                    logger.info(f"Querying Perplexity for context about: {topic}")
                    search_result = await self.perplexity_client.search(
                        query=perplexity_prompt,
                        max_tokens=500
                    )
                    if search_result.get("status") == "success":
                        context_info = search_result["data"]
                        logger.info(f"Perplexity context received:\n{context_info}")
                        logger.info("Key points from Perplexity response:")
                        for line in context_info.split('\n'):
                            if line.strip().startswith(('-', '•', '*')) or ': ' in line:
                                logger.info(f"  {line.strip()}")
                    else:
                        logger.warning(f"Perplexity search failed: {search_result.get('error')}")
                except Exception as e:
                    logger.error(f"Error getting Perplexity context: {e}")
                    context_info = ""

            # 5. Build base prompt with context
            base_prompt = f"""You are a professional social media manager crafting {count} engaging tweets about {topic}.

Latest Research Context:
{context_info}

CORE TWEET PRINCIPLES:
1. SPECIFICITY: Include precise data points, specific examples, or concrete details from the research
2. TIMELINESS: Reference exactly when information was published (e.g., "New study released today shows..." or "Breaking: As of 2PM EST...")
3. CREDIBILITY: Incorporate expert opinions with proper attribution (e.g., "According to [expert name/org]...")
4. RELEVANCE: Connect information directly to audience interests/needs/pain points
5. EMOTIONAL TRIGGERS: Evoke curiosity, surprise, concern, or excitement through unexpected facts or implications
6. ACTIONABILITY: Where possible, include a clear next step or way to use the information

TWEET FORMATS (USE VARIETY):
- Breaking news + why it matters
- Expert quote + your analysis
- Contrarian take on conventional wisdom
- Time-sensitive opportunity or deadline
- Question that challenges assumptions
- "Did you know" revelations with specific data
- Before/after or comparison frameworks

TECHNICAL REQUIREMENTS:
- Maximum 280 characters per tweet
- Include 1-2 relevant emojis per tweet (placed strategically, not decoratively)
- Incorporate contextually appropriate hashtags (max 2 per tweet)
- Vary sentence structure and length
- Use active voice and conversational tone
- Each tweet must be substantially different in content and structure"""

            if revision_instructions:
                base_prompt += f"\n\nRevision Instructions: {revision_instructions}"

            # 6. Handle regeneration if needed
            if is_regenerating:
                collecting_items = await self.tool_state_manager.get_operation_items(
                    tool_operation_id=tool_operation_id,
                    state=ToolOperationState.COLLECTING.value,
                    status=OperationStatus.PENDING.value
                )
                
                if not collecting_items:
                    raise ValueError("No regeneration items found in COLLECTING state")
                    
                logger.info(f"Found {len(collecting_items)} items to regenerate")

                # Get Perplexity context first - same as initial flow
                context_info = ""
                if self.perplexity_client:
                    try:
                        logger.info(f"Querying Perplexity for context about: {topic}")
                        search_result = await self.perplexity_client.search(
                            query=perplexity_prompt,
                            max_tokens=500
                        )
                        if search_result.get("status") == "success":
                            context_info = search_result["data"]
                            logger.info(f"Perplexity context received:\n{context_info}")
                        else:
                            logger.warning(f"Perplexity search failed: {search_result.get('error')}")
                    except Exception as e:
                        logger.error(f"Error getting Perplexity context: {e}")

                # Generate new content for each collecting item
                saved_items = []
                for item in collecting_items:
                    # Generate new content using LLM with context
                    messages = [
                        {
                            "role": "system",
                            "content": "You are a professional social media manager. Generate a single engaging tweet in strict JSON format."
                        },
                        {
                            "role": "user",
                            "content": f"""Generate a single tweet about {topic}.

{base_prompt}

{revision_instructions if revision_instructions else ''}

Return ONLY valid JSON in this format:
{{
    "content": "Tweet text here",
    "metadata": {{
        "estimated_engagement": "high/medium/low"
    }}
}}"""
                        }
                    ]
                    
                    response = await self.llm_service.get_response(
                        prompt=messages,
                        model_type=ModelType.GROQ_LLAMA_3_3_70B,
                        override_config={
                            "temperature": 0.7,
                            "max_tokens": 1000,
                            "response_format": {"type": "json_object"}
                        }
                    )
                    
                    # Parse response and update item
                    try:
                        generated_content = json.loads(response)
                        # Handle both formats - single item or array
                        if "items" in generated_content:
                            content = generated_content["items"][0]["content"]
                        else:
                            content = generated_content["content"]
                        
                        # Update the existing item with new content
                        await self.db.store_tool_item_content(
                            item_id=str(item['_id']),
                            content={
                                "raw_content": content,
                                "formatted_content": content,
                                "version": "1.0"
                            },
                            operation_details={},  # Empty dict instead of None
                            source='generate_content_regeneration',
                            tool_operation_id=tool_operation_id
                        )
                        
                        # Get updated item
                        updated_item = await self.db.tool_items.find_one({"_id": item["_id"]})
                        saved_items.append(updated_item)
                        logger.info(f"Updated content for regenerated item {item['_id']}")
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse LLM response: {e}")
                        raise

                return {
                    "items": saved_items,
                    "schedule_id": schedule_id,
                    "tool_operation_id": tool_operation_id,
                    "regeneration_needed": True,
                    "regenerate_count": len(saved_items)
                }

            else:
                # Only verify operation state for initial generation
                if operation["state"] != ToolOperationState.COLLECTING.value:
                    raise ValueError(f"Operation in invalid state for initial generation: {operation['state']}")

            # Get parent operation to inherit state/status
            operation = await self.tool_state_manager.get_operation(self.deps.session_id)
            if not operation:
                raise ValueError("No active operation found")
                
            # Update base prompt to explicitly specify count
            base_prompt = f"""You are a professional social media manager crafting EXACTLY {count} {'tweet' if count == 1 else 'tweets'} about {topic}.

IMPORTANT: Generate EXACTLY {count} tweets, no more and no less.

{context_info}

CORE TWEET PRINCIPLES:
1. SPECIFICITY: Include precise data points, specific examples, or concrete details from the research
2. TIMELINESS: Reference exactly when information was published (e.g., "New study released today shows..." or "Breaking: As of 2PM EST...")
3. CREDIBILITY: Incorporate expert opinions with proper attribution (e.g., "According to [expert name/org]...")
4. RELEVANCE: Connect information directly to audience interests/needs/pain points
5. EMOTIONAL TRIGGERS: Evoke curiosity, surprise, concern, or excitement through unexpected facts or implications
6. ACTIONABILITY: Where possible, include a clear next step or way to use the information

TWEET FORMATS (USE VARIETY):
- Breaking news + why it matters
- Expert quote + your analysis
- Contrarian take on conventional wisdom
- Time-sensitive opportunity or deadline
- Question that challenges assumptions
- "Did you know" revelations with specific data
- Before/after or comparison frameworks

TECHNICAL REQUIREMENTS:
- Maximum 280 characters per tweet
- Include 1-2 relevant emojis per tweet (placed strategically, not decoratively)
- Incorporate contextually appropriate hashtags (max 2 per tweet)
- Vary sentence structure and length
- Use active voice and conversational tone
- Each tweet must be substantially different in content and structure"""
            
            if revision_instructions:
                base_prompt += f"\n\nRevision Instructions: {revision_instructions}"
            
            prompt = f"""{base_prompt}

Guidelines:
- Generate EXACTLY {count} unique tweets
- Keep within Twitter's character limit (280 characters)
- Vary the style and tone
- Make them informative yet conversational
- Include emojis where appropriate
- No hashtags, just the content
- Ensure proper JSON formatting with commas between items

Return ONLY valid JSON with exactly {count} items in this format:
{{
    "items": [
        {{
            "content": "First tweet text",
            "metadata": {{
                "estimated_engagement": "high/medium/low"
            }}
        }}{',' if count > 1 else ''}
        {'...' if count > 1 else ''}
    ]
}}"""

            messages = [
                {
                    "role": "system",
                    "content": "You are a professional social media manager. Generate engaging tweets in strict JSON format with no trailing commas."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            logger.info(f"Sending generation prompt to LLM")
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.9,
                    "max_tokens": 1000,
                    "response_format": {"type": "json_object"}  # Request JSON format
                }
            )
            
            logger.info(f"Raw LLM response: {response}")
            
            # Clean and parse the response
            try:
                # Strip any markdown formatting
                cleaned_response = response.strip()
                if cleaned_response.startswith('```') and cleaned_response.endswith('```'):
                    cleaned_response = '\n'.join(cleaned_response.split('\n')[1:-1])
                if cleaned_response.startswith('```json'):
                    cleaned_response = cleaned_response[7:]
                
                # Remove any trailing commas before closing braces
                cleaned_response = cleaned_response.replace(',}', '}').replace(',]', ']')
                
                # Parse JSON
                generated_items = json.loads(cleaned_response)
                logger.info(f"Successfully parsed generated items")
                
                if not generated_items.get('items'):
                    raise ValueError("No items found in generated content")
                    
            except json.JSONDecodeError as e:
                logger.error(f"JSON parsing error: {e}")
                logger.error(f"Problematic response: {cleaned_response}")
                # Attempt basic error recovery
                try:
                    # Try to extract content between curly braces
                    import re
                    content_match = re.search(r'\{\s*"items"\s*:\s*\[(.*)\]\s*\}', cleaned_response, re.DOTALL)
                    if content_match:
                        items_str = content_match.group(1)
                        # Fix common JSON issues
                        items_str = items_str.replace(',,', ',').replace(',]', ']').replace(',}', '}')
                        generated_items = {"items": json.loads(f"[{items_str}]")}
                    else:
                        raise ValueError("Could not extract valid JSON structure")
                except Exception as recovery_error:
                    logger.error(f"Recovery attempt failed: {recovery_error}")
                    raise ValueError("Failed to parse tweet content") from e

            # Transform and save items with proper state inheritance
            saved_items = []
            current_pending_items = operation.get("output_data", {}).get("pending_items", [])
            
            for item in generated_items.get('items', []):
                # Fix content structure to be consistent
                content = item.get("content")
                if isinstance(content, dict):
                    # If we somehow got a nested structure, flatten it
                    content = content.get("formatted_content") or content.get("raw_content")
                
                tool_item = {
                    "session_id": self.deps.session_id,
                    "tool_operation_id": tool_operation_id,
                    "schedule_id": schedule_id,
                    "content_type": ContentType.TWEET.value,
                    "state": operation["state"],
                    "status": OperationStatus.PENDING.value,
                    "content": {
                        "raw_content": content,
                        "formatted_content": content,
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