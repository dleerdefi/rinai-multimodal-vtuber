import re
from typing import Dict, List, Optional

class TriggerDetector:
    def __init__(self):
        # Define Twitter patterns once
        self.twitter_patterns = {
            'general': {
                'keywords': ['tweet', 'twitter', '@', 'post'],
                'phrases': ['post on twitter', 'send a tweet', 'create a tweet', 'make a tweet']
            },
            'schedule': {
                'keywords': ['schedule', 'plan', 'series', 'multiple'],
                'phrases': ['schedule tweets', 'plan tweets', 'tweet series']
            },
            'immediate': {
                'keywords': ['tweet now', 'post now', 'send tweet', 'create tweet'],
                'phrases': ['tweet this', 'post this', 'send this tweet', 'create a tweet', 'make a tweet']
            }
        }
        
        # Other tool triggers
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

    def should_use_twitter(self, message: str) -> bool:
        """Check if message is Twitter-related"""
        message = message.lower()
        
        # Check all Twitter pattern categories
        for category in self.twitter_patterns.values():
            if any(keyword in message for keyword in category['keywords']) or \
               any(phrase in message for phrase in category['phrases']):
                return True
                
        return False

    def get_tool_operation_type(self, message: str) -> Optional[str]:
        """Determine if message requires a multi-step tool operation"""
        message = message.lower()
        
        if not self.should_use_twitter(message):
            return None
            
        # Check for scheduling patterns
        if any(keyword in message for keyword in self.twitter_patterns['schedule']['keywords']) or \
           any(phrase in message for phrase in self.twitter_patterns['schedule']['phrases']):
            return "schedule_tweets"
            
        # Check for immediate tweet patterns
        if any(keyword in message for keyword in self.twitter_patterns['immediate']['keywords']) or \
           any(phrase in message for phrase in self.twitter_patterns['immediate']['phrases']):
            return "send_tweet"
            
        # If it's Twitter-related but not explicitly scheduled or immediate, default to send_tweet
        return "send_tweet" 