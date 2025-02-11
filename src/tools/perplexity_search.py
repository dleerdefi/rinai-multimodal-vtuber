from datetime import datetime
import logging
from typing import Dict, Optional, Any

from src.tools.base import BaseTool
from src.clients.perplexity_client import PerplexityClient

logger = logging.getLogger(__name__)

class PerplexityTool(BaseTool):
    name = "perplexity_search"
    description = "Real-time web search and information retrieval tool"
    version = "1.0.0"
    
    def __init__(self, perplexity_client: Optional[PerplexityClient]):
        super().__init__()
        self.perplexity = perplexity_client

    def can_handle(self, input_data: Any) -> bool:
        """Check if input can be handled by perplexity tool"""
        return isinstance(input_data, str)  # Basic type check only

    async def execute(self, command: str) -> Dict:
        """Execute search command"""
        try:
            if not self.perplexity:
                return {
                    "status": "error",
                    "error": "Perplexity client not configured",
                    "timestamp": datetime.utcnow().isoformat()
                }
            
            result = await self.perplexity.search(command)
            return {
                "status": "success",
                "data": result,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error executing perplexity search: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Main execution method"""
        return await self.search(input_data)

    async def search(self, query: str, max_tokens: int = 300) -> Dict:
        """Execute search query"""
        try:
            if not self.perplexity:
                return {
                    "status": "error",
                    "error": "Perplexity client not configured",
                    "timestamp": datetime.utcnow().isoformat()
                }
                
            result = await self.perplexity.search(query, max_tokens)
            return {
                "status": "success",
                "data": result,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error in perplexity search: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            } 