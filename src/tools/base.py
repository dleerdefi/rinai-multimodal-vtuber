from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import asyncio
from datetime import datetime
from pydantic import BaseModel, Field

class BaseTool(ABC):
    """Base class for all tools"""
    name: str
    description: str
    version: str
    
    def __init__(self):
        self.cache = {}
        self.cache_ttl = 300  # 5 minutes default
        
    @abstractmethod
    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Execute the tool's main functionality"""
        pass
    
    @abstractmethod
    def can_handle(self, input_data: Any) -> bool:
        """Check if this tool can handle the given input"""
        pass

    async def get_cached_or_fetch(self, key: str, fetch_func) -> Any:
        """Generic caching mechanism for tools"""
        now = datetime.now().timestamp()
        if key in self.cache:
            if now - self.cache[key]['timestamp'] < self.cache_ttl:
                return self.cache[key]['data']
        
        data = await fetch_func()
        self.cache[key] = {
            'data': data,
            'timestamp': now
        }
        return data

class ToolCommand(BaseModel):
    """Structure for tool commands"""
    tool_name: str = Field(description="Name of tool to execute")
    action: str = Field(description="Action to perform")
    parameters: Dict = Field(default={}, description="Tool parameters")
    priority: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Execution priority (1-5)"
    )

class CommandAnalysis(BaseModel):
    """AI model for analyzing commands"""
    tools_needed: List[ToolCommand] = Field(description="Tools required for this command")
    reasoning: str = Field(description="Explanation of tool selection")

class AgentResult(BaseModel):
    """Universal result structure for all agents"""
    response: str = Field(description="Response to the command/query")
    target_agent: Optional[str] = Field(
        description="Agent to delegate to",
        default=None
    )
    data: Optional[Dict] = Field(
        description="Structured data from tool execution",
        default=None
    )

class AgentDependencies(BaseModel):
    """Shared dependencies across all agents"""
    conversation_id: str
    user_id: Optional[str]
    context: Optional[Dict] = {}
    tools_available: List[str] = []

class TweetApprovalAnalysis(BaseModel):
    """Model for tweet approval command analysis"""
    action: str = Field(description="Action to take: full_approval | partial_approval | regenerate_all | partial_regenerate")
    approved_indices: List[int] = Field(description="List of approved tweet numbers from 1 to N")
    regenerate_indices: List[int] = Field(description="List of tweet numbers to regenerate from 1 to N")
    feedback: str = Field(description="Explanation in Rin's voice")

class TweetContent(BaseModel):
    """Model for individual tweet content"""
    content: str = Field(description="Content of the tweet")

class TweetGenerationResponse(BaseModel):
    """Model for LLM tweet generation response"""
    tweets: List[TweetContent] = Field(description="List of generated tweets")

# TODO: Add weather tool parameters
# class WeatherToolParameters(BaseModel):
#     """Parameters for the weather tool function call."""
#     location: str = Field(..., description="City or place name. E.g., 'Berlin' or 'New York'.")