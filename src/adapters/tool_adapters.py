from src.tools.base import BaseTool
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

class TwitterToolAdapter:
    """Adapter for Twitter tool scheduling"""
    
    def __init__(self, twitter_tool: BaseTool):
        self.twitter_tool = twitter_tool
        
    async def execute_scheduled_operation(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """Custom implementation for Twitter scheduled operations"""
        try:
            # Extract content from the operation
            content = operation.get('content', {}).get('raw_content')
            if not content:
                content = operation.get('content', {}).get('formatted_content')
            
            if not content:
                raise ValueError("No content found for scheduled tweet")

            # Extract parameters specific to Twitter
            params = {
                'account_id': operation.get('metadata', {}).get('account_id', 'default'),
                'media_files': operation.get('metadata', {}).get('media_files', []),
                'poll_options': operation.get('metadata', {}).get('poll_options', [])
            }
            
            # Get the client from the tool
            twitter_client = self.twitter_tool.deps.clients.get('twitter_client')
            if not twitter_client:
                raise ValueError("Twitter client not available")
            
            # Send the tweet
            result = await twitter_client.send_tweet(content=content, params=params)
            
            return {
                'success': result.get('success', False),
                'id': result.get('id'),
                'text': content,
                'timestamp': datetime.now(UTC).isoformat(),
                'result': result
            }
            
        except Exception as e:
            logger.error(f"Error executing scheduled tweet: {e}")
            return {
                'success': False,
                'error': str(e)
            } 