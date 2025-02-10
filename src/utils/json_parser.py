import json
import logging
from typing import Optional, Type, TypeVar
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)

def extract_json(response: str) -> str:
    """Extract JSON substring from text"""
    # Handle markdown code blocks first
    if "```json" in response:
        response = response.split("```json")[1].split("```")[0].strip()
    elif "```" in response:
        response = response.split("```")[1].strip()
        
    start_idx = response.find('{')
    end_idx = response.rfind('}')
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        return ""
    return response[start_idx:end_idx+1]

def parse_strict_json(response: str, model_cls: Type[T]) -> Optional[T]:
    """Parse LLM response into Pydantic model"""
    try:
        json_str = extract_json(response)
        if not json_str:
            logger.error("No JSON found in response")
            return None
            
        logger.debug(f"Attempting to parse JSON: {json_str}")
        raw_data = json.loads(json_str)
        return model_cls(**raw_data)
        
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error(f"Failed to parse/validate JSON: {e}")
        return None 