import re
from typing import Dict, List, Optional

class TriggerDetector:
    def __init__(self):
        # Initialize the tool_triggers dictionary first
        self.tool_triggers = {}
        
        # Define Twitter patterns once
        self.twitter_patterns = {
            'general': {
                'keywords': ['tweet', 'twitter', '@', 'post'],
                'phrases': [
                    'post on twitter', 'post on x', 'send a tweet', 'create a tweet', 'make a tweet',
                    'write a tweet', 'compose a tweet', 'publish a tweet'
                ]
            },

            'schedule': {
                'keywords': ['schedule', 'plan', 'series', 'multiple', 'timed', 'batch'],
                'phrases': [
                    'schedule tweets', 'schedule a tweet', 'schedule two tweets', 'schedule three tweets', 'schedule four tweets', 'schedule five tweets', 'schedule six tweets', 'schedule seven tweets', 'schedule eight tweets', 'schedule nine tweets', 'schedule ten tweets', 'schedule 1 tweet', 'schedule 2 tweets', 'schedule 3 tweets', 'schedule 4 tweets', 'schedule 5 tweets', 'schedule 6 tweets', 'schedule 7 tweets', 'schedule 8 tweets', 'schedule 9 tweets', 'schedule 10 tweets', 'plan tweets', 'tweet series', 'queue tweets',
                    'automate tweets', 'post later'
                ]
            },
            'immediate': {
                'keywords': ['tweet now', 'post now', 'send tweet', 'create tweet', 'publish now'],
                'phrases': [
                    'tweet this', 'post this', 'send this tweet', 'create a tweet', 'make a tweet'
                ]
            },
            'reply': {
                'keywords': ['reply', 'respond', 'comment', 'thread'],
                'phrases': [
                    'reply to this', 'respond to this tweet', 'add a comment', 'continue this thread'
                ]
            },
            'retweet': {
                'keywords': ['retweet', 'rt', 'share'],
                'phrases': [
                    'retweet this', 'quote tweet this', 'share this tweet'
                ]
            },
            'like': {
                'keywords': ['like', 'favorite'],
                'phrases': [
                    'like this tweet', 'favorite this', 'add to liked tweets'
                ]
            },
            'hashtag': {
                'keywords': ['#', 'hashtag'],
                'phrases': [
                    'add hashtag', 'include hashtag', 'use the hashtag'
                ]
            },
            'engagement': {
                'keywords': ['analytics', 'metrics', 'stats', 'engagement', 'reach'],
                'phrases': [
                    'how many likes', 'tweet stats', 'twitter engagement', 'show tweet performance'
                ]
            }
        }
        
        # Crypto Tool Triggers
        self.tool_triggers['crypto'] = {
            'keywords': [
                'bitcoin', 'btc', 'eth', 'solana', 'near', 'dogecoin', 'fartcoin', 'sui', 'ethereum', 'price', 'market', 'crypto', '$', 'coin',
                'token', 'altcoin', 'nft', 'blockchain', 'gas fee', 'wallet', 'swap', 'defi'
            ],

            'phrases': [
                'how much is', "what's the price", 'show me the market', 'crypto trends',
            ]
        }
        
        # Web Search with Reasoning Tool Triggers
        self.tool_triggers['search'] = {
            'keywords': [
                'news', 'latest', 'current', 'today', 'happened', 'recent', 'headline',
                'stocks', 'finance', 'updates', 'economy', 'elections', 'company', 'ceo',
                'IPO', 'merger', 'lawsuit', 'AI developments'
            ],
            'phrases': [
                'what is happening', 'tell me about', 'what happened', 'search for',
                'explain this news', 'show me headlines', 'recent updates', 'trending stories'
            ]
        }

        # GraphRAG Memory triggers
        self.memory_triggers = {
            'keywords': ['remember', 'you said', 'earlier', 'before', 'last time', 'previously', 'we talked about', 'remind me'],
            'phrases': [
                'do you recall', 'as we discussed', 'like you mentioned', 'remind me what we said about',
                'bring up my last request', 'what did I say before', 'you told me earlier'
            ]
        }

    def should_use_tools(self, message: str) -> bool:
        """Check if message should trigger tool usage"""
        message = message.lower()
        
        for tool in self.tool_triggers.values():
            # Check keywords
            if any(keyword.lower() in message for keyword in tool['keywords']):
                return True
                
            # Check phrases
            if any(phrase.lower() in message for phrase in tool['phrases']):
                return True
                
        return False
        
    def should_use_memory(self, message: str) -> bool:
        """Check if message should trigger memory lookup"""
        message = message.lower()
        
        # Check memory keywords
        if any(keyword.lower() in message for keyword in self.memory_triggers['keywords']):
            return True
            
        # Check memory phrases
        if any(phrase.lower() in message for phrase in self.memory_triggers['phrases']):
            return True
            
        return False

    def should_use_twitter(self, message: str) -> bool:
        """Check if message is Twitter-related"""
        message = message.lower()
        
        # Check all Twitter pattern categories
        for category in self.twitter_patterns.values():
            if any(keyword.lower() in message for keyword in category['keywords']) or \
               any(phrase.lower() in message for phrase in category['phrases']):
                return True
                
        return False

    def get_tool_operation_type(self, message: str) -> Optional[str]:
        """Determine specific Twitter operation type from message"""
        message = message.lower()
        
        if not self.should_use_twitter(message):
            return None
            
        # Check for scheduling patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['schedule']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['schedule']['phrases']):
            return "schedule_tweets"
            
        # Check for immediate tweet patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['immediate']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['immediate']['phrases']):
            return "send_tweet"
            
        # Check for reply patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['reply']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['reply']['phrases']):
            return "reply_tweet"
            
        # Check for retweet patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['retweet']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['retweet']['phrases']):
            return "retweet"
            
        # Check for like patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['like']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['like']['phrases']):
            return "like_tweet"
            
        # Check for engagement/analytics patterns
        if any(keyword.lower() in message for keyword in self.twitter_patterns['engagement']['keywords']) or \
           any(phrase.lower() in message for phrase in self.twitter_patterns['engagement']['phrases']):
            return "show_analytics"
            
        # If it's Twitter-related but not matching specific patterns, default to send_tweet
        return "send_tweet"

    def get_specific_tool_type(self, message: str) -> Optional[str]:
        """Determine specific tool type needed"""
        message = message.lower()
        
        # Check crypto triggers
        if any(keyword.lower() in message for keyword in self.tool_triggers['crypto']['keywords']) or \
           any(phrase.lower() in message for phrase in self.tool_triggers['crypto']['phrases']):
            return "crypto_data"
            
        # Check search triggers
        if any(keyword.lower() in message for keyword in self.tool_triggers['search']['keywords']) or \
           any(phrase.lower() in message for phrase in self.tool_triggers['search']['phrases']):
            return "perplexity_search"
            
        return None 