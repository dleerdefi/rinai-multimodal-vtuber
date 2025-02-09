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