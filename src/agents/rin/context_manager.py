"""
Copyright (c) 2025 dleerdefi & Aggregated AI
Licensed under the MIT License. See LICENSE in the project root for license information.
"""

from typing import List, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorClient
import logging
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential
from src.db.db_schema import RinDB
from src.services.llm_service import LLMService, ModelType
import asyncio
import tiktoken
from src.db.mongo_manager import MongoManager


logger = logging.getLogger(__name__)

class RinContext:
    # Revised token management constants
    MAX_CONTEXT_TOKENS = 16000  # Llama-2 70B max context
    TOKEN_THRESHOLD = 12800     # 80% of max context (trigger summarization)
    SUMMARY_TOKEN_TARGET = 4096  # Target 25% for summary
    RETENTION_TOKEN_TARGET = 4096  # Target 25% for retained messages

    SUMMARY_COOLDOWN = 300      # 5 minutes between summarizations
    
    def __init__(self, mongo_uri: str):
        self.mongo_uri = mongo_uri
        self.db = None
        self._initialized = False
        self._active_stream_id = None
        self.battle_contexts = {}
        self.llm_service = LLMService({
            "model_type": ModelType.GROQ_LLAMA_3_3_70B
        })
        # Initialize tokenizer
        self.enc = tiktoken.get_encoding("cl100k_base")
        
    async def initialize(self):
        """Initialize database and setup indexes with retry"""
        try:
            # Use MongoManager instead of direct client creation
            await MongoManager.initialize(self.mongo_uri)
            self.db = MongoManager.get_db()
            self._initialized = True
            logger.info("Successfully initialized RinContext")
        except Exception as e:
            logger.error(f"Failed to initialize RinContext: {e}")
            raise

    async def is_initialized(self) -> bool:
        return self._initialized
    
    @retry(wait=wait_exponential(multiplier=1, min=2, max=6), stop=stop_after_attempt(3))
    async def store_interaction(self, session_id: str, user_message: str, 
                              assistant_response: str, interaction_type: str = 'chat',
                              metadata: Optional[dict] = None):
        """Store only essential interaction data"""
        try:
            # Store minimal metadata
            essential_metadata = {
                'type': interaction_type
            } if metadata else None
            
            # Store messages with minimal metadata
            await self.db.add_message(
                session_id=session_id,
                content=user_message,
                role='user',
                metadata=essential_metadata
            )
            await self.db.add_message(
                session_id=session_id,
                content=assistant_response,
                role='assistant',
                metadata=essential_metadata
            )
        except Exception as e:
            logger.error(f"Failed to store interaction: {e}")
            raise
    
    async def summarize_conversation_context(self, session_id: str) -> bool:
        """Summarize conversation and update context without deleting messages"""
        try:
            messages = await self.db.get_session_messages(session_id)
            total_tokens = await self._count_tokens(session_id)
            
            # Calculate retention based on tokens
            retained_messages = []
            retained_token_count = 0
            
            # Work backwards from newest messages
            for msg in reversed(messages):
                msg_tokens = len(self.enc.encode(msg['content'])) + 3
                if retained_token_count + msg_tokens <= self.RETENTION_TOKEN_TARGET:
                    retained_messages.insert(0, msg)
                    retained_token_count += msg_tokens
                else:
                    break
                    
            # Everything else gets summarized
            messages_to_summarize = [msg for msg in messages if msg not in retained_messages]
            
            if not messages_to_summarize:
                logger.warning("No messages to summarize")
                return False
                
            # Format messages for summarization
            formatted_chat = "\n".join([
                f"{msg['role'].upper()}: {msg['content']}"
                for msg in messages_to_summarize
            ])
            
            summary_prompt = [{
                "role": "system",
                "content": """You are an expert summarizer agent working for Rin, a loyal and intelligent girlfriend style AI companion. When summarizing conversations:
                - Maintain Rin's cheerful, flirty, and caring personality
                - Focus on key information, personal details, tool outputs and emotional context
                - Keep summaries clear and well-structured
                
                Create a summary that Rin can reference in future conversations to maintain context and connection with the user."""
            }, {
                "role": "user",
                "content": f"Please summarize this conversation history while maintaining Rin's personality:\n\n{formatted_chat}"
            }]

            # Generate summary using Groq
            final_summary = await self.llm_service.get_response(
                prompt=summary_prompt,
                model_type=ModelType.GROQ_LLAMA_3_3_70B,
                override_config={
                    "temperature": 0.3,
                    "max_tokens": 1000,
                    "top_p": 0.9
                }
            )
            
            if not final_summary:
                logger.error("Failed to generate summary")
                return False
            
            summary_tokens = len(self.enc.encode(final_summary))
            
            # Create summary message
            summary_msg = {
                "role": "system",
                "content": final_summary,
                "metadata": {
                    "type": "conversation_summary",
                    "summarized_message_ids": [str(msg["_id"]) for msg in messages_to_summarize],
                    "summary_timestamp": datetime.utcnow(),
                    "original_message_count": len(messages_to_summarize),
                    "summary_tokens": summary_tokens,
                    "retained_tokens": retained_token_count,
                    "total_tokens_before": total_tokens
                }
            }
            
            # Update context configuration ONLY
            await self.db.add_context_summary(
                session_id, 
                summary_msg,
                [str(msg["_id"]) for msg in retained_messages]
            )
            
            new_total = summary_tokens + retained_token_count
            logger.info(f"Summarization complete: {total_tokens} → {new_total} tokens "
                       f"({(new_total/total_tokens)*100:.1f}% of original)")
            
            return True
            
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return False

    def _validate_summary(self, original_messages: List[Dict], summary: str) -> bool:
        """Validate summary quality"""
        if not summary or len(summary) < 50:
            return False
        
        # Check for key content preservation
        key_phrases = self._extract_key_phrases(original_messages)
        return any(phrase.lower() in summary.lower() for phrase in key_phrases)

    async def _count_tokens(self, session_id: str) -> int:
        """Count tokens in active context only (summary + retained messages + new messages)"""
        try:
            # Get current context configuration
            config = await self.db.get_context_configuration(session_id)
            total_tokens = 0
            
            if config:
                # Count summary tokens if exists
                if config.get('latest_summary'):
                    summary_content = config['latest_summary']['content']
                    total_tokens += len(self.enc.encode(summary_content))
                
                # Get timestamp of last summary
                last_summary_time = config.get('latest_summary', {}).get('metadata', {}).get('summary_timestamp')
                
                if last_summary_time:
                    # Get all messages after the last summary
                    new_messages = await self.db.messages.find({
                        "session_id": session_id,
                        "timestamp": {"$gt": last_summary_time}
                    }).to_list(None)
                    
                    # Count tokens for new messages
                    for msg in new_messages:
                        total_tokens += len(self.enc.encode(msg['content'])) + 3
                    
                    # Also count retained messages from before summary
                    if config.get('active_message_ids'):
                        retained_msgs = await self.db.get_messages_by_ids(
                            session_id, 
                            config['active_message_ids']
                        )
                        for msg in retained_msgs:
                            if msg.get('timestamp') <= last_summary_time:
                                total_tokens += len(self.enc.encode(msg['content'])) + 3
                else:
                    # If no summary timestamp, count all messages
                    messages = await self.db.get_session_messages(session_id)
                    for msg in messages:
                        total_tokens += len(self.enc.encode(msg['content'])) + 3
                
            else:
                # If no config exists yet, count all messages (initial state)
                messages = await self.db.get_session_messages(session_id)
                for msg in messages:
                    total_tokens += len(self.enc.encode(msg['content'])) + 3
                
            return total_tokens
            
        except Exception as e:
            logger.error(f"Failed to count tokens: {e}")
            return 0

    async def get_combined_context(self, session_id: str, current_message: str = None) -> List[Dict]:
        """Get context with active messages and summaries"""
        try:
            # Get current context configuration
            context_config = await self.db.get_context_configuration(session_id)
            
            if not context_config:
                # No summaries yet, return full recent context
                messages = await self.db.get_session_messages(session_id)
                # Handle limiting in memory
                messages = messages[-20:] if len(messages) > 20 else messages
                return [{"role": msg["role"], "content": msg["content"]} for msg in messages]
            
            # Get active messages and latest summary
            active_messages = await self.db.get_messages_by_ids(
                session_id, 
                context_config["active_message_ids"]
            )
            
            # Construct context with summary and active messages
            context = []
            if context_config["latest_summary"]:
                context.append(context_config["latest_summary"])
            context.extend([
                {"role": msg["role"], "content": msg["content"]} 
                for msg in active_messages
            ])
            
            return context
            
        except Exception as e:
            logger.error(f"Failed to get combined context: {e}")
            return []

    async def clear_session(self, session_id: str):
        """Clear all messages for a session"""
        try:
            await self.db.clear_session(session_id)
            logger.info(f"Cleared session {session_id}")
        except Exception as e:
            logger.error(f"Failed to clear session {session_id}: {e}")
            raise

    async def get_session_history(self, session_id: str) -> List[Dict]:
        """Get session message history from database"""
        try:
            messages = await self.db.get_session_messages(session_id)
            return [
                {
                    'role': msg['role'],
                    'content': msg['content'],
                    'timestamp': msg['timestamp']
                }
                for msg in messages
            ] if messages else []
        except Exception as e:
            logger.error(f"Failed to get session history: {e}")
            return []