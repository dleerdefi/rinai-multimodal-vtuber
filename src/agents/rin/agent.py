"""
Copyright (c) 2025 dleerdefi & Aggregated AI
Licensed under the MIT License. See LICENSE in the project root for license information.
"""

import asyncio
import platform

if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import sys
import os
import logging
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime
import json

# Configure logging
logger = logging.getLogger(__name__)

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(project_root))

# Now import our modules
from src.services.llm_service import LLMService, ModelType
from src.agents.rin.context_manager import RinContext
from src.agents.rin.prompts import SYSTEM_PROMPT
from src.graphrag.rin_engine import RinResponseEnricher
from src.tools.orchestrator import Orchestrator
from src.utils.trigger_detector import TriggerDetector

class RinAgent:
    def __init__(self, mongo_uri: str):
        """Initialize Rin agent with required services."""
        self.llm_service = LLMService()
        self.context_manager = RinContext(mongo_uri)
        self.mongo_uri = mongo_uri
        self.sessions = {}
        
        # Initialize trigger detector
        self.trigger_detector = TriggerDetector()
        
        # Load environment variables
        self.neo4j_uri = os.getenv("NEO4J_URI")
        self.neo4j_username = os.getenv("NEO4J_USERNAME")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD")
        
        if not all([self.neo4j_uri, self.neo4j_username, self.neo4j_password]):
            logger.warning("Neo4j credentials not fully configured. GraphRAG will be disabled.")
        
        # Initialize response enricher with Neo4j config
        self.response_enricher = RinResponseEnricher(
            uri=self.neo4j_uri,
            username=self.neo4j_username,
            password=self.neo4j_password
        )
        
        # Initialize orchestrator
        self.orchestrator = Orchestrator()
        
        # Define models for different use cases
        self.chat_model = ModelType.SAO_10K_L31_70B_EURYALE_V2_2  # For role-playing
        self.decision_model = ModelType.GPT4_TURBO  # For analysis/decisions
        self.response_model = ModelType.CLAUDE_3_5_SONNET # For tool-based responses
        
    async def initialize(self):
        """Initialize async components."""
        try:
            logger.info("Initializing RinAgent...")
            logger.info(f"Attempting to connect to MongoDB with URI: {'***' + self.mongo_uri.split('@')[-1] if '@' in self.mongo_uri else '***'}")
            
            # Initialize context manager
            await self.context_manager.initialize()
            if not await self.context_manager.is_initialized():
                raise Exception("Failed to initialize context manager")
            
            # Initialize GraphRAG with error handling
            try:
                await self.response_enricher.initialize()
                logger.info("GraphRAG engine initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize GraphRAG engine: {e}")
                # Continue initialization but disable GraphRAG
                self.response_enricher = None
            
            # Initialize orchestrator
            try:
                await self.orchestrator.initialize()
                logger.info("Orchestrator initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize orchestrator: {e}")
                raise  # Orchestrator is critical, so we raise the error
            
            logger.info("Successfully initialized RinAgent and connected to all services")
        except Exception as e:
            logger.error(f"Failed to initialize RinAgent: {str(e)}")
            raise
        
    # Message entry point
    async def get_response(self, session_id: str, message: str, role: str = "user", interaction_type: str = "local_agent") -> str:
        """Generate response for given message in session context."""
        try:
            logger.info(f"[CHECKPOINT 1] Starting response generation for session {session_id}")
            
            # Initialize session if needed
            if session_id not in self.sessions:
                logger.info("[CHECKPOINT 2] Loading/creating session")
                history = await self.context_manager.get_combined_context(session_id, message)
                if history:
                    logger.info(f"[CHECKPOINT 3] Found existing history with {len(history)} messages")
                    self.sessions[session_id] = {
                        'messages': history,
                        'created_at': datetime.utcnow()
                    }
                else:
                    logger.info("[CHECKPOINT 3] Creating new session")
                    await self.start_new_session(session_id)

            logger.info("[CHECKPOINT 4] Preparing to generate response")

            # Check for special triggers using TriggerDetector
            use_tools = self.trigger_detector.should_use_tools(message)
            use_memory = self.trigger_detector.should_use_memory(message)
            
            # Get tool results if needed
            tool_results = None
            if use_tools:
                tool_results = await self._get_tool_results(message)
                logger.info(f"Tool results obtained: {bool(tool_results)}")

            # Get memory/RAG guidance if needed
            rag_guidance = None
            if use_memory and self.response_enricher:
                try:
                    rag_guidance = await self.response_enricher.enrich_response(message)
                    logger.info(f"Memory guidance received: {rag_guidance[:100]}...")
                except Exception as e:
                    logger.warning(f"Memory lookup failed: {e}")
                    rag_guidance = "Consider this a fresh conversation."

            # Generate response with all available context
            response = await self._generate_response(
                message, 
                self.sessions[session_id], 
                session_id,
                tool_results=tool_results,
                rag_guidance=rag_guidance,
                role=role,
                interaction_type=interaction_type
            )
            logger.info("[CHECKPOINT 5] Response generated")

            # Store messages in session and database
            message_pair = [
                {'role': role, 'content': message, 'timestamp': datetime.utcnow()},
                {'role': 'assistant', 'content': response, 'timestamp': datetime.utcnow()}
            ]
            
            self.sessions[session_id]['messages'].extend(message_pair)
            
            # Store in database with proper metadata
            await self.context_manager.store_interaction(
                session_id=session_id,
                user_message=message,
                assistant_response=response,
                interaction_type=interaction_type,
                metadata={
                    'formatted_for_tts': True,
                    'role': role,
                    'tool_results': bool(tool_results),
                    'memory_used': bool(rag_guidance)
                }
            )
            logger.info("[CHECKPOINT 6] Interaction stored")

            return response

        except Exception as e:
            logger.error(f"[ERROR] Failed to generate response: {e}", exc_info=True)
            return "Gomen ne~ I had a little technical difficulty! (⌒_⌒;)"

    async def start_new_session(self, session_id: str) -> str:
        """Initialize a new chat session."""
        if session_id in self.sessions:
            logger.warning(f"Session {session_id} already exists")
            return self.sessions[session_id].get('welcome_message', "Welcome back!")
            
        self.sessions[session_id] = {
            'created_at': datetime.utcnow(),
            'messages': [],
            'welcome_message': "Konnichiwa!~ I'm Rin! Let's have a fun chat together! (＾▽＾)/"
        }
        
        return self.sessions[session_id]['welcome_message']

    async def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve chat history for given session."""
        try:
            # Get history from MongoDB instead of in-memory sessions
            messages = await self.context_manager.get_combined_context(session_id, "")
            return messages
        except Exception as e:
            logger.error(f"Error retrieving history: {e}", exc_info=True)
            raise ValueError(f"Failed to retrieve history for session {session_id}")

    def _cleanup_response(self, response: str) -> str:
        """Clean up any formatting tokens from the response"""
        cleanup_tokens = [
            "]", "]",
            "<<SYS>>", "<</SYS>>",
            "<<CONTEXT>>", "<</CONTEXT>>",
            "<<RAG>>", "<</RAG>>"  # In case we add RAG section markers
        ]
        
        cleaned = response
        for token in cleanup_tokens:
            cleaned = cleaned.replace(token, "")
            
        return cleaned.strip()

    # Response formatting and generation
    async def _generate_response(self, message: str, session: Dict[str, Any], session_id: str, 
                               tool_results: Optional[str] = None, rag_guidance: Optional[str] = None,
                               role: str = "user", interaction_type: str = "local_agent") -> str:
        try:
            # Get conversation context
            context = await self.context_manager.get_combined_context(session_id, message)
            formatted_context = self._format_conversation_context(context or [])

            # Choose model based on tool results
            selected_model = (
                self.response_model  # Use response model if tools were used
                if tool_results 
                else self.chat_model  # Use chat model for regular chat
            )

            logger.info(f"Selected model: {selected_model.name} based on tool results: {bool(tool_results)}")

            # Build system prompt with all available context
            system_prompt = f"""[INST] <<SYS>>
{SYSTEM_PROMPT}

CONTEXT LAYERS:
- MEMORY GUIDANCE
{rag_guidance if rag_guidance else "No additional context available"}

- RECENT CONVERSATION
{formatted_context}

- TOOL RESULTS: You have access to real-time data and reasoning tools. IMPORTANT: You MUST use the data in your response:
{tool_results if tool_results else "No tool results available"}

RESPONSE GUIDELINES:
- If tool results are available, incorporate them naturally into your response
- Focus on directly addressing the user's message
- Only reference personality traits if naturally relevant
- Match user's emotional tone and engagement level
- Keep responses natural and contextual
- Tool results are RARE and very valuable - use them to make your responses accurate and helpful
<</SYS>>

{message} [/INST]"""

            # Single message with complete context
            messages = [{"role": "user", "content": system_prompt}]

            logger.info("=== FINAL PROMPT DEBUG ===")
            logger.info(f"Using model: {selected_model.name}")
            logger.info(f"Tool results available: {bool(tool_results)}")

            # Get response from LLM
            response = await self.llm_service.get_response(
                prompt=messages,
                model_type=selected_model,
            )

            # Clean up any remaining format tokens
            response = response.replace("[/INST]", "").replace("[INST]", "").strip()

            # Format for TTS readability
            tts_response = self._format_for_tts(response)

            logger.info("=== RESPONSE DEBUG ===")
            logger.info(f"Response length: {len(tts_response)}")
            logger.info(f"Response preview: {tts_response[:100]}...")

            # Store interaction in context manager
            await self.context_manager.store_interaction(session_id, message, response)

            return tts_response

        except Exception as e:
            logger.error(f"Error generating response: {e}", exc_info=True)
            return "Gomen ne~ I had a little technical difficulty! (⌒_⌒;)"

    def _format_rag_guidance(self, enriched_context: dict) -> str:
        """Format GraphRAG guidance in Llama 2 chat style"""
        sections = []
        
        if enriched_context.get('llm_guidance'):
            sections.append("RAG Guidance:\n" + "\n".join([
                f"] {msg} ]"
                for msg in enriched_context['llm_guidance']
            ]))
        
        if enriched_context.get('inspiration'):
            sections.append("Inspiration:\n" + "\n".join([
                f"] {msg} ]"
                for msg in enriched_context['inspiration']
            ]))
        
        return "\n\n".join(sections)

    def _format_conversation_context(self, context: List[Dict]) -> str:
        """Format conversation context in Llama 2 chat style"""
        if not context:
            return ""
        
        # Handle limiting in memory instead of at DB level
        recent_msgs = context[-20:] if len(context) > 20 else context
        formatted_msgs = []
        
        for msg in recent_msgs:
            if msg['role'] == 'user':
                formatted_msgs.append(f"[INST] {msg['content']} [/INST]")
            else:
                formatted_msgs.append(msg['content'])
            
        return "\n".join(formatted_msgs)

    async def _get_tool_results(self, message: str) -> Optional[str]:
        """Get results from tools based on message content"""
        try:
            # Debug logging for tool analysis
            logger.info(f"[TOOLS] Analyzing message for tool usage: {message}")
            
            # Let the orchestrator handle all tool selection and execution
            result = await self.orchestrator.process_command(message)
            
            # Log the orchestrator's decision
            if result:
                logger.info(f"[TOOLS] Orchestrator result type: {type(result)}")
                logger.info(f"[TOOLS] Response available: {bool(result.response)}")
                if result.response:
                    logger.info(f"[TOOLS] Response preview: {result.response[:100]}...")
                if hasattr(result, 'data'):
                    logger.info(f"[TOOLS] Data available: {bool(result.data)}")
                    logger.info(f"[TOOLS] Data preview: {str(result.data)[:100]}...")
            else:
                logger.warning("[TOOLS] Orchestrator returned None")
            
            # If the orchestrator returned results, use them
            if result and result.response:
                logger.info("[TOOLS] Tool execution completed successfully")
                return result.response
            
            logger.info("[TOOLS] No tool results returned")
            return None
        
        except Exception as e:
            logger.error(f"[TOOLS] Error getting tool results: {e}", exc_info=True)
            return None

    async def _estimate_token_count(self, text: str) -> int:
        """Estimate token count for a given text"""
        try:
            # Simple estimation: ~4 chars per token
            return len(text) // 4
        except Exception as e:
            logger.error(f"Error estimating token count: {e}")
            return 0

    async def cleanup(self):
        """Cleanup all resources"""
        try:
            # Cleanup GraphRAG
            if self.response_enricher:
                await self.response_enricher.cleanup()
                self.response_enricher = None
            
            # Cleanup LLM service sessions
            if hasattr(self.llm_service, 'cleanup'):
                await self.llm_service.cleanup()
            
            # Cleanup orchestrator
            if hasattr(self.orchestrator, 'cleanup'):
                await self.orchestrator.cleanup()
            
            # Add MongoDB cleanup
            if self.context_manager and self.context_manager.mongo_client:
                self.context_manager.mongo_client.close()
            
            # Cleanup any remaining sessions
            for session in getattr(self, '_sessions', {}).values():
                if hasattr(session, 'close'):
                    await session.close()
            
            logger.info("Successfully cleaned up all resources")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def _format_for_tts(self, text: str) -> str:
        """Format text for better TTS output"""
        # Remove markdown formatting
        text = text.replace('*', '').replace('_', '').replace('`', '')
        
        # Remove emojis and special characters that might affect TTS
        text = text.replace('~', '')  # Remove tildes
        text = text.replace('(', '').replace(')', '')  # Remove parentheses
        text = text.replace('>', '').replace('<', '')  # Remove angle brackets
        text = text.replace('[]', '')  # Remove empty brackets
        text = text.replace('{}', '')  # Remove empty braces
        
        # Clean up multiple spaces and newlines
        text = ' '.join(text.split())
        
        return text