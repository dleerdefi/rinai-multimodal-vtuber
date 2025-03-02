from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Literal
import asyncio
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from src.db.db_schema import ContentType, ToolType

class ToolRegistry(BaseModel):
    """Tool registration configuration"""
    content_type: ContentType
    tool_type: ToolType
    requires_approval: bool = True
    requires_scheduling: bool = False
    required_clients: List[str] = []
    required_managers: List[str] = []

class BaseTool(ABC):
    """Base class for all tools"""
    name: str
    description: str
    version: str
    registry: ToolRegistry
    
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

    @classmethod
    def get_registry(cls) -> ToolRegistry:
        """Get tool registration info"""
        return cls.registry

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
    session_id: str
    user_id: Optional[str] = None
    context: Optional[Dict] = {}
    tools_available: List[str] = []
    agent: Optional[Any] = None  # Add agent field for user interaction

class TweetApprovalAnalysis(BaseModel):
    """Model for tweet approval command analysis"""
    action: str = Field(description="Action to take: full_approval | partial_approval | regenerate_all | partial_regenerate")
    approved_indices: List[int] = Field(description="List of approved tweet numbers from 1 to N")
    regenerate_indices: List[int] = Field(description="List of tweet numbers to regenerate from 1 to N")
    feedback: str = Field(description="Explanation in Rin's voice")

class TweetContent(BaseModel):
    """Model for individual tweet content"""
    content: str = Field(..., max_length=280)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

class TweetGenerationResponse(BaseModel):
    """Model for LLM tweet generation response"""
    tweets: List[TweetContent] = Field(description="List of generated tweets")

    def to_dict(self) -> Dict:
        """Convert to dictionary for database storage"""
        return self.dict(exclude_none=True)

class TimeToolParameters(BaseModel):
    """Parameters for time tool operations"""
    timezone: str = Field(description="IANA timezone string (e.g., 'America/New_York')")
    action: Literal["get_time", "convert_time"] = Field(description="Time operation to perform")
    source_time: Optional[str] = Field(None, description="Source time for conversion")
    source_timezone: Optional[str] = Field(None, description="Source timezone for conversion")

class WeatherToolParameters(BaseModel):
    """Parameters for weather tool operations"""
    location: str = Field(description="Location to get weather for")
    units: Literal["metric", "imperial"] = Field(
        default="metric",
        description="Units system to use"
    )

class CryptoToolParameters(BaseModel):
    """Parameters for crypto tool operations"""
    symbol: str = Field(description="Cryptocurrency symbol (e.g., BTC, ETH)")
    include_details: bool = Field(
        default=False,
        description="Whether to include detailed metrics"
    )

class SearchToolParameters(BaseModel):
    """Parameters for search tool operations"""
    query: str = Field(description="Search query string")
    max_tokens: int = Field(
        default=300,
        description="Maximum response length"
    )

class CalendarToolParameters(BaseModel):
    """Parameters for calendar tool operations"""
    max_events: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of events to fetch"
    )
    time_min: Optional[str] = Field(
        default=None,
        description="Start time for event fetch (ISO format)"
    )
    time_max: Optional[str] = Field(
        default=None,
        description="End time for event fetch (ISO format)"
    )

class ToolParameters(BaseModel):
    """Base parameters for all tools"""
    schedule_time: Optional[datetime] = Field(default=None)
    retry_policy: Optional[Dict] = Field(default=None)
    execution_window: Optional[Dict] = Field(default=None)
    custom_params: Dict[str, Any] = Field(default_factory=dict)

class TwitterParameters(ToolParameters):
    """Twitter-specific tool parameters - matches TwitterParams TypedDict"""
    custom_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Twitter API and content parameters"
    )

    class Config:
        schema_extra = {
            "example": {
                "custom_params": {
                    # API Parameters
                    "account_id": "default",
                    "media_files": [],
                    "poll_options": [],
                    "poll_duration": None,
                    "reply_settings": None,
                    "quote_tweet_id": None,
                    
                    # Content Parameters
                    "thread_structure": [],
                    "mentions": [],
                    "hashtags": [],
                    "urls": [],
                    
                    # Targeting Parameters
                    "audience_targeting": {},
                    "content_category": None,
                    "sensitivity_level": None,
                    
                    # Engagement Parameters
                    "estimated_engagement": None,
                    "visibility_settings": {}
                }
            }
        }

class ToolOperation(BaseModel):
    """Model for tool operations"""
    session_id: str
    tool_type: str
    state: str
    step: str
    parameters: ToolParameters  # Updated to use new parameters model
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    last_updated: datetime
    end_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary for database storage"""
        return self.dict(exclude_none=True)

class SchedulableToolInterface(ABC):
    """Interface for tools that can be scheduled"""
    
    @abstractmethod
    async def execute_scheduled_operation(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a scheduled operation"""
        pass

class LimitOrderToolParameters(BaseModel):
    """Parameters for limit order operations"""
    from_token: str = Field(description="Token to sell (e.g., NEAR, ETH)")
    from_amount: float = Field(description="Amount to sell")
    to_token: str = Field(description="Token to buy (e.g., USDC, ETH)")
    target_price_usd: float = Field(description="Target price in USD")
    to_chain: str = Field(
        default="eth",
        description="Destination chain for the output token"
    )
    expiration_hours: int = Field(
        default=24,
        description="Hours until order expires"
    )
    destination_address: Optional[str] = Field(
        default=None,
        description="Optional destination address for withdrawal"
    )
    destination_chain: Optional[str] = Field(
        default=None,
        description="Optional destination chain for withdrawal"
    )