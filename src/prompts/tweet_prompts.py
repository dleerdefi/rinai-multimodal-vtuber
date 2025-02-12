# not in use yet
# _analyze_twitter_command = 
"""You are a Twitter action analyzer. Determine the specific Twitter action needed.

Command: "{command}"

Available Twitter actions: 
1. send_tweet: Post a new tweet immediately
   Parameters: message, account_id (optional)

2. schedule_tweets: Schedule one or more tweets for later
   Parameters: 
   - tweet_count: number of tweets to schedule
   - topic: what to tweet about
   - schedule_type: "one_time"
   - schedule_time: when to post (default: spread over next 24 hours)
   - approval_required: true

Instructions:
- Return ONLY valid JSON matching the example format
- Extract count, topic, and timing information from command
- If no specific time mentioned, default to spreading tweets over next 24 hours
- Include schedule_time in parameters

Example response format:
{{
    "tools_needed": [{{
        "tool_name": "twitter",
        "action": "schedule_tweets",
        "parameters": {{
            "tweet_count": 5,
            "topic": "artificial intelligence",
            "schedule_type": "one_time",
            "schedule_time": "spread_24h",
            "approval_required": true
        }},
        "priority": 1
    }}],
    "reasoning": "User requested scheduling multiple tweets about AI"
}}
"""

# _generate_tweet_series = 
"""You are a tweet generator. Generate a series of tweets about a given topic.
                    {
                        "role": "system",
                        "content": "You are an expert tweet creator. Create tweet(s) about the topic provided. Return ONLY valid JSON."
                    },
                    {
                        "role": "user",
                        "content": f"""Generate {'a single' if count == 1 else str(count)} engaging tweet{'s' if count > 1 else ''} about {topic}.
                        
Requirements:
- {'Each tweet must be' if count > 1 else 'Must be'} under 280 characters
- Maintain a {tone} tone
- Be engaging and natural
- Return as JSON: {{"tweets": [{{"content": "tweet text"}}]}}
"""

# _analyze_tweets_and_response =
"""Analyze user's response to tweets"""
        try:
            presentation = self._format_tweets_for_presentation(tweets)
            
            prompt = [
                {
                    "role": "system",
                    "content": "Analyze user instructions on how to proceed with the proposed draft tweet(s) and return structured JSON."
                },
                {
                    "role": "user",
                    "content": f"""Context: Previous tweets presented:
{presentation}

User response: "{user_response}"

There are {len(tweets)} tweets to analyze. Return ONLY valid JSON in this exact format:
{{
    "action": "full_approval" | "partial_approval" | "regenerate_all" | "partial_regenerate",
    "approved_indices": [list of approved tweet numbers from 1 to {len(tweets)}],
    "regenerate_indices": [list of tweet numbers to regenerate from 1 to {len(tweets)}],
    "feedback": "explanation in Rin's voice"
}}
"""