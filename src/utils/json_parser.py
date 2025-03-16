import json
import logging
from typing import Optional, Type, TypeVar
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)

def extract_json(response: str) -> str:
    """Extract JSON substring from text, handling various LLM response formats"""
    try:
        # Strip whitespace
        response = response.strip()
        
        # Handle markdown code blocks
        if "```" in response:
            # Split on code block markers and get the content
            blocks = response.split("```")
            # Get the block that contains json (usually the second block)
            for block in blocks:
                # Remove "json" language identifier if present
                if block.startswith("json"):
                    block = block[4:].strip()
                elif block.startswith("javascript"):
                    block = block[10:].strip()
                    
                # Try to find JSON markers - handle both objects and arrays
                if ("[" in block and "]" in block) or ("{" in block and "}" in block):
                    # For arrays
                    if block.find('[') < block.find('{') or (block.find('[') != -1 and block.find('{') == -1):
                        start_idx = block.find('[')
                        end_idx = block.rfind(']')
                    # For objects
                    else:
                        start_idx = block.find('{')
                        end_idx = block.rfind('}')
                        
                    if start_idx != -1 and end_idx > start_idx:
                        return block[start_idx:end_idx + 1].strip()
        
        # If no code blocks, try to find JSON markers directly
        # Check for arrays first
        start_idx = response.find('[')
        end_idx = response.rfind(']')
        
        # If no array markers, try object markers
        if start_idx == -1 or end_idx == -1:
            start_idx = response.find('{')
            end_idx = response.rfind('}')
            
        if start_idx != -1 and end_idx > start_idx:
            return response[start_idx:end_idx + 1].strip()
            
        logger.warning(f"No valid JSON structure found in response: {response[:100]}...")
        return ""
        
    except Exception as e:
        logger.error(f"Error extracting JSON: {e}")
        return ""

def parse_strict_json(response: str, model_cls: Optional[Type[T]] = None) -> Optional[T]:
    """Parse LLM response into Pydantic model or dict with enhanced error handling"""
    try:
        json_str = extract_json(response)
        if not json_str:
            logger.error("No JSON found in response")
            return None
            
        logger.debug(f"Attempting to parse JSON: {json_str}")
        raw_data = json.loads(json_str)
        
        # If no model class specified or it's dict, return raw data
        if not model_cls or model_cls == dict:
            return raw_data
            
        try:
            validated = model_cls(**raw_data)
            logger.debug(f"Validated data: {validated}")
            return validated
        except ValidationError as ve:
            logger.error(f"Validation error: {ve}")
            return raw_data
            
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return None 