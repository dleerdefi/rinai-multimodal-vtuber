import re
from typing import Dict, List, Optional

class TriggerDetector:
    def __init__(self):
        # Tool triggers
        self.tool_triggers = {
            'crypto': {
                'keywords': ['bitcoin', 'btc', 'eth', 'ethereum', 'price', 'market', 'crypto', '$'],
                'phrases': ['how much is', "what's the price", 'show me the market']
            },
            'search': {
                'keywords': ['news', 'latest', 'current', 'today', 'happened', 'recent'],
                'phrases': ['what is happening', 'tell me about', 'what happened', 'search for']
            }
        }

        # Memory triggers
        self.memory_triggers = {
            'keywords': ['remember', 'you said', 'earlier', 'before', 'last time', 'previously'],
            'phrases': ['do you recall', 'as we discussed', 'like you mentioned']
        }

    def should_use_tools(self, message: str) -> bool:
        """Check if message should trigger tool usage"""
        message = message.lower()
        
        for tool in self.tool_triggers.values():
            # Check keywords
            if any(keyword in message for keyword in tool['keywords']):
                return True
                
            # Check phrases
            if any(phrase in message for phrase in tool['phrases']):
                return True
                
        return False
        
    def should_use_memory(self, message: str) -> bool:
        """Check if message should trigger memory lookup"""
        message = message.lower()
        
        # Check memory keywords
        if any(keyword in message for keyword in self.memory_triggers['keywords']):
            return True
            
        # Check memory phrases
        if any(phrase in message for phrase in self.memory_triggers['phrases']):
            return True
            
        return False 