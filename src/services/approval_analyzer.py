from datetime import datetime, UTC
import json
import logging
from typing import Dict, List, Any, Optional
from src.services.llm_service import LLMService, ModelType
from src.db.enums import OperationStatus, ContentType, ToolOperationState

logger = logging.getLogger(__name__)

class ApprovalAnalyzer:
    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    async def analyze_response(
        self,
        user_response: str,
        current_items: List[Dict]
    ) -> Dict[str, Any]:
        """Analyze user's approval response"""
        try:
            logger.info(f"Analyzing approval response: {user_response}")
            
            # Filter for only current turn's pending items in APPROVING state
            valid_items = [
                item for item in current_items 
                if (item.get('status') == OperationStatus.PENDING.value and
                    item.get('state') == ToolOperationState.APPROVING.value and
                    item.get('content') and 
                    not item.get('metadata', {}).get('rejected_at'))
            ]
            
            logger.info(f"Found {len(valid_items)} active pending items for current turn")
            
            presentation = self.format_items_for_review(valid_items)
            
            prompt = [
                {
                    "role": "system",
                    "content": """You analyze user responses about content approval. Handle natural language responses flexibly.

Key actions to detect:
- Full approval: "looks good", "approve all", "yes", etc.
- Partial approval: "approve first one", "like 1 and 3", etc.
- Rejection/Regeneration: "reject this", "redo item 2", "regenerate", etc.
- Exit/Cancel: "stop", "cancel", "quit", etc.

Always return structured JSON with the appropriate action and indices."""
                },
                {
                    "role": "user",
                    "content": f"""Context: User is reviewing {len(valid_items)} items.

Items being reviewed:
{presentation}

User's response: "{user_response}"

Return JSON in this format:
{{
    "action": "full_approval" | "partial_approval" | "regenerate_all" | "exit",
    "approved_indices": [item numbers approved],
    "regenerate_indices": [item numbers to regenerate],
    "feedback": "explanation of action taken",
    "revision_instructions": "specific feedback about what to change or fix in regenerated items"
}}

Example:
Input: "reject these because we need tweets about swordfish not anglerfish"
Output: {{
    "action": "regenerate_all",
    "approved_indices": [],
    "regenerate_indices": [1, 2],
    "feedback": "All items rejected for regeneration",
    "revision_instructions": "Generate tweets about swordfish instead of anglerfish"
}}"""
                }
            ]

            response = await self.llm_service.get_response(
                prompt=prompt,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"}
                }
            )

            # Parse and validate the response
            analysis = json.loads(response)
            
            # Ensure indices are within bounds of current pending items
            if 'approved_indices' in analysis:
                analysis['approved_indices'] = [
                    idx for idx in analysis['approved_indices'] 
                    if 1 <= idx <= len(valid_items)
                ]
            
            if 'regenerate_indices' in analysis:
                analysis['regenerate_indices'] = [
                    idx for idx in analysis['regenerate_indices'] 
                    if 1 <= idx <= len(valid_items)
                ]
            
            # Add metadata about the analysis context
            analysis['metadata'] = {
                'total_pending_items': len(valid_items),
                'analyzed_at': datetime.now(UTC).isoformat(),
                'valid_item_ids': [str(item.get('_id')) for item in valid_items]
            }
            
            logger.info(f"Processed analysis: {analysis}")
            return analysis

        except Exception as e:
            logger.error(f"Error analyzing approval response: {e}")
            return {"action": "error", "feedback": str(e)}

    def _build_analysis_prompt(self, user_response: str, current_items: List[Dict]) -> str:
        """Build prompt for analyzing user response"""
        return f"""Analyze the following user response to {len(current_items)} generated items:
        "{user_response}"
        
        Return a JSON object with the following structure:
        {{
            "action": "full_approval" | "partial_approval" | "regenerate_all" | "exit" | "cancel" | "stop" | "error",
            "approved_indices": [list of approved item indices, 1-based],
            "regenerate_indices": [list of indices to regenerate, 1-based]
        }}
        
        Guidelines:
        - For partial approvals, extract specific item numbers mentioned
          * "approve first one" → [1]
          * "approve items 1 and 3" → [1, 3]
          * "approve the first and last ones" → [1, {len(current_items)}]
        - Numbers in approved_indices should be between 1 and {len(current_items)}
        - If no specific items mentioned but action is partial_approval, include all mentioned items
        
        Current response: "{user_response}"
        """

    def create_error_analysis(self, error_message: str, is_retryable: bool = True) -> Dict:
        """Create error analysis response with retry information"""
        return {
            "action": "error",
            "approved_indices": [],
            "regenerate_indices": [],
            "feedback": f"Error analyzing response: {error_message}",
            "is_retryable": is_retryable,
            "metadata": {
                "error_time": datetime.now(UTC).isoformat(),
                "error_type": "retryable" if is_retryable else "terminal"
            }
        }

    def create_response(
        self,
        status: str,
        message: str,
        requires_tts: bool = True,
        data: Optional[Dict] = None
    ) -> Dict:
        """Create a standardized response"""
        response = {
            "status": status,
            "response": message,
            "requires_tts": requires_tts
        }
        if data:
            response["data"] = data
        return response

    def create_error_response(
        self, 
        message: str, 
        is_retryable: bool = True,
        retry_count: Optional[int] = None
    ) -> Dict:
        """Create an error response with retry information"""
        return self.create_response(
            "error",
            message,
            data={
                "is_retryable": is_retryable,
                "retry_count": retry_count,
                "error_time": datetime.now(UTC).isoformat()
            }
        )

    def create_awaiting_response(self) -> Dict:
        """Create an awaiting input response"""
        return self.create_response(
            "awaiting_input",
            "I'm not sure what you'd like to do. Please clarify.",
            requires_tts=True
        )

    def create_exit_response(
        self,
        success: bool,
        tool_type: str
    ) -> Dict:
        """Create exit response"""
        exit_details = self._get_exit_details(success)
        return self.create_response(
            status="completed" if success else "cancelled",
            message=exit_details["exit_message"],
            data={
                "tool_type": tool_type,
                "completion_type": "success" if success else "cancelled",
                "final_status": exit_details["status"]
            }
        )

    def _get_exit_details(self, success: bool) -> Dict:
        """Get exit details based on success"""
        return {
            "reason": "Operation completed successfully" if success else "Operation failed with error",
            "status": OperationStatus.APPROVED.value if success else OperationStatus.FAILED.value,
            "exit_message": (
                "Great! All done. What else would you like to discuss?"
                if success else
                "I encountered an error. Let's try something else. What would you like to do?"
            )
        }

    def format_items_for_review(self, items: List[Dict]) -> str:
        """Format items for user review with content-type specific handling"""
        try:
            logger.info(f"Formatting {len(items)} items for review")
            review_text = "Here are the items for your review:\n\n"
            
            for i, item in enumerate(items, 1):
                logger.info(f"Processing item {i} structure: {json.dumps(item.get('content', {}), indent=2)}")
                
                content = item.get('content', {})
                content_type = item.get('content_type')
                
                review_text += f"Item {i}:\n"
                
                if content_type == ContentType.LIMIT_ORDER.value:
                    # Format limit order content
                    review_text += self._format_limit_order(content)
                elif content_type == ContentType.TWEET.value:
                    # Format tweet content
                    review_text += self._format_tweet(content)
                else:
                    # Generic content formatting
                    review_text += self._format_generic_content(content)
                
                review_text += "\n"
                
            # Add standard options menu
            review_text += "Would you like to:\n"
            review_text += "Approve all, regenerate all, regenerate specific items, or cancel and exit?\n"
            
            return review_text

        except Exception as e:
            logger.error(f"Error formatting items for review: {e}")
            return f"Error formatting items: {str(e)}"

    def _format_limit_order(self, content: Dict) -> str:
        """Format limit order specific content"""
        # Log full details for console
        logger.info(f"Formatting limit order content: {json.dumps(content, indent=2)}")
        
        # For TTS, only include essential info
        text = ""
        op_details = content.get('operation_details', {})
        if op_details:
            text += f"Limit order to swap {op_details.get('from_amount')} {op_details.get('from_token')} "
            text += f"for {op_details.get('to_token')} when {op_details.get('reference_token')} "
            text += f"reaches ${op_details.get('target_price_usd')}.\n"
        
        return text

    def _format_tweet(self, content: Dict) -> str:
        """Format tweet specific content"""
        text = ""
        if content.get('raw_content'):
            text += f"Tweet: {content['raw_content']}\n"
        elif content.get('formatted_content'):
            text += f"Tweet: {content['formatted_content']}\n"
        
        # Add any tweet-specific metadata
        metadata = content.get('metadata', {})
        if metadata:
            if metadata.get('estimated_engagement'):
                text += f"Estimated Engagement: {metadata['estimated_engagement']}\n"
            if metadata.get('scheduled_time'):
                text += f"Scheduled Time: {metadata['scheduled_time']}\n"
        
        return text

    def _format_generic_content(self, content: Dict) -> str:
        """Format generic content when specific formatter not available"""
        if isinstance(content, dict):
            # Try to extract meaningful fields
            text = ""
            for key, value in content.items():
                if key not in ['metadata', 'version']:  # Skip technical fields
                    text += f"{key.replace('_', ' ').title()}: {value}\n"
            return text
        else:
            # Fallback for simple string content
            return f"{content}\n" 