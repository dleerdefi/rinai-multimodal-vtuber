from datetime import datetime, UTC
import json
import logging
from typing import Dict, List, Any, Optional
from src.services.llm_service import LLMService, ModelType
from src.db.db_schema import OperationStatus

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
            # Build presentation of items
            presentation = self.format_items_for_review(current_items)
            
            prompt = [
                {
                    "role": "system",
                    "content": "Analyze user instructions on how to proceed with the proposed items and return structured JSON."
                },
                {
                    "role": "user",
                    "content": f"""Context: Previous items presented:
{presentation}

User response: "{user_response}"

There are {len(current_items)} items to analyze. Return ONLY valid JSON in this exact format:
{{
    "action": "full_approval" | "partial_approval" | "regenerate_all" | "exit" | "cancel" | "stop" | "error",
    "approved_indices": [list of approved item numbers from 1 to {len(current_items)}],
    "regenerate_indices": [list of item numbers to regenerate from 1 to {len(current_items)}],
    "feedback": "explanation of the action taken"
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

            # Parse response
            analysis = json.loads(response)
            logger.info(f"LLM analysis result: {analysis}")
            
            # Convert to 0-based indices
            approved_indices = [i-1 for i in analysis.get("approved_indices", [])]
            regenerate_indices = [i-1 for i in analysis.get("regenerate_indices", [])]
            
            logger.info(f"Processing approval with {len(approved_indices)} approved and {len(regenerate_indices)} regenerate items")
            
            # Validate indices are within bounds
            approved_indices = [i for i in approved_indices if 0 <= i < len(current_items)]
            regenerate_indices = [i for i in regenerate_indices if 0 <= i < len(current_items)]
            
            return {
                "action": analysis.get("action", "error"),
                "indices": approved_indices,
                "items": current_items,
                "regenerate_indices": regenerate_indices,
                "feedback": analysis.get("feedback", ""),
                "metadata": {
                    "approval_type": analysis.get("action"),
                    "analyzed_at": datetime.now(UTC).isoformat(),
                    "original_response": user_response,
                    "item_count": len(current_items),
                    "approved_count": len(approved_indices),
                    "regenerate_count": len(regenerate_indices)
                }
            }
        except Exception as e:
            logger.error(f"Error analyzing approval response: {e}")
            return {
                "action": "error",
                "error": str(e),
                "metadata": {
                    "analyzed_at": datetime.now(UTC).isoformat(),
                    "original_response": user_response
                }
            }

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
        """Format items for user review"""
        try:
            logger.info(f"Formatting {len(items)} items for review")
            review_text = "Here are the items for your review:\n\n"
            
            for i, item in enumerate(items, 1):
                # Handle both direct content and nested content structure
                if isinstance(item.get('content'), str):
                    content = item['content']
                else:
                    content = item.get('content', {}).get('raw_content', '')
                
                logger.info(f"Formatting item {i}: {content[:50]}...")
                review_text += f"Item {i}:\n{content}\n\n"
                
            review_text += "\nWould you like to:\n"
            review_text += "1. Approve all items\n"
            review_text += "2. Approve specific items (e.g., 'approve items 1 and 3')\n"
            review_text += "3. Regenerate all items\n"
            review_text += "4. Regenerate specific items (e.g., 'regenerate item 2')\n"
            review_text += "5. Cancel the operation\n"
            
            return review_text

        except Exception as e:
            logger.error(f"Error formatting items for review: {e}")
            return f"Error formatting items: {str(e)}" 