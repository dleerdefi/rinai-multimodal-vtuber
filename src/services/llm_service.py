from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
import os
import logging
from enum import Enum
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from openai import OpenAI
from anthropic import AsyncAnthropic
import json
from groq import AsyncGroq

load_dotenv()

logger = logging.getLogger(__name__)

class ModelType(Enum):
    # OpenAI Models
    GPT4o = "gpt-4o"
    GPT4_TURBO = "gpt-4-turbo-preview"
    GPT4 = "gpt-4"
    GPT35_TURBO = "gpt-3.5-turbo"
    # Claude Models
    CLAUDE_3_5_SONNET = "claude-3-5-sonnet-20240620"
    CLAUDE_3_OPUS = "claude-3-opus-latest"
    CLAUDE_3_5_HAIKU = "claude-3-5-haiku-latest"
    # Together AI Models
    LLAMA_3_8B = "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
    MIXTRAL_8x7B = "mistralai/Mixtral-8x7B-Instruct-v0.1"
    SOLAR_10_7B = "upstage/SOLAR-10.7B-Instruct-v1.0"
    # Novita Models
    SAO_10K_L31_70B_EURYALE_V2_2 = "sao10k/l31-70b-euryale-v2.2"
    # Groq Models
    GROQ_LLAMA_3_2_3B = "llama2-3b-preview-8k"
    GROQ_LLAMA_3_2_70B = "llama2-70b-preview-8k" 
    GROQ_LLAMA_3_3_70B = "llama-3.3-70b-versatile"  
    # Atoma Models
    ATOMA_LLAMA_3_3_70B = "meta-llama/Llama-3.3-70B-Instruct"

class LLMProvider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    TOGETHER = "together"
    NOVITA = "novita"
    GROQ = "groq"  
    ATOMA = "atoma"  

class LLMService:
    def __init__(self, llm_settings=None):
        # Initialize API keys
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')
        self.together_api_key = os.getenv('TOGETHER_API_KEY')
        self.novita_api_key = os.getenv('NOVITA_API_KEY')
        self.groq_api_key = os.getenv('GROQ_API_KEY')  
        self.atoma_api_key = os.getenv('ATOMA_API_KEY')  

        # Set model type from settings or use default
        self.model_type = (llm_settings or {}).get("model_type", ModelType.CLAUDE_3_5_SONNET)
        
        # Map models to their providers
        self.model_providers = {
            # OpenAI Models
            ModelType.GPT4o: LLMProvider.OPENAI,
            ModelType.GPT4_TURBO: LLMProvider.OPENAI,
            ModelType.GPT4: LLMProvider.OPENAI,
            ModelType.GPT35_TURBO: LLMProvider.OPENAI,
            # Claude Models
            ModelType.CLAUDE_3_OPUS: LLMProvider.ANTHROPIC,
            ModelType.CLAUDE_3_5_SONNET: LLMProvider.ANTHROPIC,
            ModelType.CLAUDE_3_5_HAIKU: LLMProvider.ANTHROPIC,
            # Together AI Models
            ModelType.LLAMA_3_8B: LLMProvider.TOGETHER,
            ModelType.MIXTRAL_8x7B: LLMProvider.TOGETHER,
            ModelType.SOLAR_10_7B: LLMProvider.TOGETHER,
            # Novita Models
            ModelType.SAO_10K_L31_70B_EURYALE_V2_2: LLMProvider.NOVITA,
            # Groq Models
            ModelType.GROQ_LLAMA_3_2_3B: LLMProvider.GROQ,
            ModelType.GROQ_LLAMA_3_2_70B: LLMProvider.GROQ,
            ModelType.GROQ_LLAMA_3_3_70B: LLMProvider.GROQ,
            # Atoma Models
            ModelType.ATOMA_LLAMA_3_3_70B: LLMProvider.ATOMA,
        }

        # Default configurations for different models with config types
        self.model_configs = {
            # OpenAI configs
            ModelType.GPT4o: {
                "default": {"temperature": 0.7, "max_tokens": 500},
                "json": {
                    "temperature": 0.2,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"}
                },
                "decision": {"temperature": 0.2, "max_tokens": 100}
            },
            ModelType.GPT4_TURBO: {
                "default": {"temperature": 0.7, "max_tokens": 500},
                "decision": {"temperature": 0.2, "max_tokens": 100}
            },
            ModelType.GPT4: {
                "default": {"temperature": 0.7, "max_tokens": 50},
                "decision": {"temperature": 0.2, "max_tokens": 100}
            },
            ModelType.GPT35_TURBO: {
                "default": {"temperature": 0.9, "max_tokens": 500},
                "decision": {"temperature": 0.3, "max_tokens": 100}
            },
            # Claude configs
            ModelType.CLAUDE_3_OPUS: {
                "default": {"temperature": 0.7, "max_tokens": 500},
                "decision": {"temperature": 0.2, "max_tokens": 100}
            },
            ModelType.CLAUDE_3_5_SONNET: {
                "default": {"temperature": 0.7, "max_tokens": 500},
                "decision": {"temperature": 0.2, "max_tokens": 100}
            },
            ModelType.CLAUDE_3_5_HAIKU: {
                "default": {"temperature": 0.9, "max_tokens": 600},
                "decision": {"temperature": 0.3, "max_tokens": 100}
            },
            # Together AI configs
            ModelType.LLAMA_3_8B: {
                "default": {"temperature": 0.7, "max_tokens": 1000}
            },
            ModelType.MIXTRAL_8x7B: {
                "default": {"temperature": 0.7, "max_tokens": 1000}
            },
            ModelType.SOLAR_10_7B: {
                "default": {"temperature": 0.7, "max_tokens": 1000}
            },
            # Novita configs
            ModelType.SAO_10K_L31_70B_EURYALE_V2_2: {
                "default": {
                    "temperature": 0.88,
                    "top_p": 0.9,
                    "max_tokens": 300,
                    "stream": False,
                    "response_format": {"type": "text"}
                }
            },
            # Groq configs
            ModelType.GROQ_LLAMA_3_2_3B: {
                "default": {
                    'temperature': 0.1,  # Low temperature for classification
                    'max_tokens': 50,    # Short responses for classification
                    'top_p': 0.9,
                    'stream': False
                }
            },
            ModelType.GROQ_LLAMA_3_2_70B: {
                "default": {
                    'temperature': 0.1,
                    'max_tokens': 50,
                    'top_p': 0.9,
                    'stream': False
                }
            },
            ModelType.GROQ_LLAMA_3_3_70B: {
                "default": {
                    "temperature": 0.88,  # Low for classification
                    "max_tokens": 500,    # Short responses for classification
                    "top_p": 0.9
                }
            },
            # Atoma configs
            ModelType.ATOMA_LLAMA_3_3_70B: {
                "default": {
                    "temperature": 0.88,
                    "top_p": 0.9,
                    "min_p": 0.05,
                    "top_k": 50,
                    "presence_penalty": 0.3,
                    "frequency_penalty": 0.3,
                    "repetition_penalty": 1.1,
                    "max_tokens": 900,
                    "stream": False,
                    "response_format": {"type": "text"}
                }
            },
        }

    async def get_response(
        self, 
        prompt: str | list,
        model_type: Optional[ModelType] = None,
        override_config: Optional[Dict[str, Any]] = None,
        config_type: str = "default"
    ) -> str:
        try:
            model_type = model_type or self.model_type
            provider = self.model_providers[model_type]
            
            # Get the appropriate config based on type
            model_config = self.model_configs[model_type]
            if isinstance(model_config, dict) and config_type in model_config:
                config = model_config[config_type].copy()
            else:
                config = model_config["default"].copy()
                
            if override_config:
                config.update(override_config)
            
            logger.debug(f"Using provider: {provider}")
            logger.debug(f"Model type: {model_type}")
            logger.debug(f"Config type: {config_type}")
            logger.debug(f"Final config: {config}")
            
            if provider == LLMProvider.OPENAI:
                return await self._get_openai_response(prompt, model_type, config)
            elif provider == LLMProvider.ANTHROPIC:
                return await self._get_claude_response(prompt, model_type, config)
            elif provider == LLMProvider.TOGETHER:
                return await self._get_together_response(prompt, model_type, config)
            elif provider == LLMProvider.NOVITA:
                return await self._get_novita_response(prompt, model_type, config)
            elif provider == LLMProvider.GROQ:
                return await self._get_groq_response(prompt, model_type, config)
            elif provider == LLMProvider.ATOMA:
                return await self._get_atoma_response(prompt, model_type, config)
            
        except Exception as e:
            logger.error(f"Error getting LLM response: {str(e)}", exc_info=True)
            logger.error(f"Provider: {provider}")
            logger.error(f"Model: {model_type}")
            logger.error(f"Prompt: {prompt}")
            return "Sorry, I encountered an error processing your request."

    def _prepare_messages(self, prompt: str | list, provider: LLMProvider) -> list:
        """Prepare messages based on provider and input type"""
        if provider == LLMProvider.ANTHROPIC:
            return self._prepare_claude_messages(prompt)
        elif provider == LLMProvider.OPENAI:
            return self._prepare_openai_messages(prompt)
        elif provider == LLMProvider.TOGETHER:
            return self._prepare_together_messages(prompt)
        elif provider == LLMProvider.ATOMA:
            return self._prepare_atoma_messages(prompt)


    async def _get_openai_response(self, prompt: str | list, model_type: ModelType, config: Dict) -> str:
        messages = self._prepare_openai_messages(prompt)
        validated_messages = self._validate_messages(messages)
        
        # Create client with specific model
        client = ChatOpenAI(
            api_key=self.openai_api_key,
            model=model_type.value,
            temperature=config.get('temperature', 0.85),
            max_tokens=config.get('max_tokens', 1200)
        )
        
        response = await client.ainvoke(validated_messages)
        return response.content

    async def _get_claude_response(self, prompt: str | list, model_type: ModelType, config: Dict) -> str:
        try:
            messages = self._prepare_claude_messages(prompt)
            
            print("\n=== CLAUDE API DEBUG ===")
            print(f"API Key length: {len(self.anthropic_api_key) if self.anthropic_api_key else 'None'}")
            print(f"Model: {model_type.value}")
            
            if not self.anthropic_api_key:
                raise ValueError("Anthropic API key not found")

            client = AsyncAnthropic(api_key=self.anthropic_api_key)
            
            try:
                # Extract system message
                system_message = None
                user_messages = []
                
                for msg in messages:
                    if msg['role'] == 'system':
                        system_message = msg['content']
                    else:
                        user_messages.append(msg)
                
                if not system_message:
                    system_message = "You are Rin, a cute VTuber who streams about crypto."
                
                print("\nFormatted for Claude API:")
                print(f"System: {system_message}")
                print(f"Messages: {json.dumps(user_messages, indent=2)}")
                
                message = await client.messages.create(
                    model=model_type.value,
                    system=system_message,  # Pass system message separately
                    messages=[
                        {'role': msg['role'], 'content': msg['content']}
                        for msg in user_messages  # Only pass non-system messages
                    ],
                    temperature=config.get('temperature', 0.7),
                    max_tokens=config.get('max_tokens', 500)
                )
                
                print("\nClaude API call successful!")
                print(f"Response content: {message.content if message.content else 'No content'}")
                
                if not message.content:
                    raise ValueError("Empty response from Claude")
                    
                return message.content[0].text
                
            except Exception as e:
                print(f"\nClaude API call failed: {str(e)}")
                print(f"Error type: {type(e).__name__}")
                if hasattr(e, 'response'):
                    print(f"Response status: {e.response.status_code if hasattr(e.response, 'status_code') else 'N/A'}")
                    print(f"Response body: {await e.response.text() if hasattr(e.response, 'text') else 'N/A'}")
                raise
            
        except Exception as e:
            print(f"\nError in Claude response generation: {str(e)}")
            raise

    async def _get_together_response(self, prompt: str | list, model_type: ModelType, config: Dict) -> str:
        messages = self._prepare_together_messages(prompt)
        validated_messages = self._validate_messages(messages)
        
        logger.debug(f"Sending messages to Together AI {model_type.value} with config: {config}")
        
        response = self.together_client.chat.completions.create(
            model=model_type.value,
            messages=validated_messages,
            **config
        )
        
        return response.choices[0].message.content

    async def _get_novita_response(self, messages: str | list, model_type: ModelType, config: Dict) -> str:
        try:
            # Reset conversation if there are errors
            if isinstance(messages, list):
                system_msg = next((msg for msg in messages if msg['role'] == 'system'), None)
                user_msg = next((msg for msg in reversed(messages) if msg['role'] == 'user'), None)
                
                if system_msg and user_msg:
                    messages = [system_msg, user_msg]
            
            # Create client
            client = OpenAI(
                base_url="https://api.novita.ai/v3/openai",
                api_key=self.novita_api_key
            )
            
            logger.debug(f"Sending request with {len(messages)} messages")
            logger.debug(f"Using config: {config}")
            
            completion = client.chat.completions.create(
                model=model_type.value,
                messages=messages,
                **config  # Use the passed config which comes from model_configs
            )
            
            return completion.choices[0].message.content

        except Exception as e:
            logger.error(f"Error in Novita response: {str(e)}", exc_info=True)
            return f"API Error: {str(e)}"

    def _prepare_openai_messages(self, prompt: str | list) -> list:
        if isinstance(prompt, str):
            return [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant that creates engaging Twitter content. Keep responses concise and suitable for Twitter's format."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        return prompt

    def _prepare_claude_messages(self, prompt: str | list) -> list:
        if isinstance(prompt, str):
            return [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant that creates engaging Twitter content. Keep responses concise and suitable for Twitter's format."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        elif isinstance(prompt, list):
            # Ensure messages are in the correct format
            formatted_messages = []
            for msg in prompt:
                if isinstance(msg, dict) and 'role' in msg and 'content' in msg:
                    if msg['role'] in ['system', 'user', 'assistant']:
                        formatted_messages.append(msg)
            return formatted_messages
        return []

    def _prepare_together_messages(self, prompt: str | list) -> list:
        if isinstance(prompt, str):
            return [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant that creates engaging Twitter content. Keep responses concise and suitable for Twitter's format."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        return prompt
    
    def _prepare_novita_messages(self, messages: list) -> list:
        if isinstance(messages, str):
            return [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        return prompt

    def _validate_messages(self, messages: list) -> list:
        validated_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                logger.warning(f"Invalid message format: {msg}")
                continue
                
            if 'role' not in msg or 'content' not in msg:
                logger.warning(f"Missing required fields in message: {msg}")
                continue
                
            if msg['role'] not in ['system', 'user', 'assistant']:
                logger.warning(f"Invalid role in message: {msg}")
                continue
                
            validated_messages.append({
                'role': msg['role'],
                'content': str(msg['content'])
            })

        if not validated_messages:
            raise ValueError("No valid messages to send to OpenAI")
            
        return validated_messages

    def _prepare_atoma_messages(self, prompt: str | list) -> list:
        """Format messages for Atoma API"""
        if isinstance(prompt, str):
            return [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant powered by Llama 3.3 70B. Provide clear and concise responses."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        elif isinstance(prompt, list):
            formatted_messages = []
            for msg in prompt:
                if isinstance(msg, dict) and 'role' in msg and 'content' in msg:
                    if msg['role'] in ['system', 'user', 'assistant']:
                        formatted_messages.append(msg)
            return formatted_messages
        return []

    async def _get_atoma_response(self, prompt: str | list, model_type: ModelType, config: Dict) -> str:
        """Handle Atoma API requests"""
        try:
            messages = self._prepare_atoma_messages(prompt)
            validated_messages = self._validate_messages(messages)
            
            client = OpenAI(
                base_url="https://api.atoma.network/v1",  # Updated API endpoint
                api_key=self.atoma_api_key
            )
            
            logger.debug(f"Sending request to Atoma {model_type.value}")
            
            # Prepare config with required fields
            api_config = {
                "stream": False,  # We're not handling streaming for now
                "model": model_type.value,
                "temperature": config.get('temperature', 0.88),  # Updated to match our config
                "max_tokens": config.get('max_tokens', 900)      # Updated to match our config
            }
            
            # Remove await since this client doesn't support it
            completion = client.chat.completions.create(
                messages=validated_messages,
                **api_config
            )
            
            return completion.choices[0].message.content

        except Exception as e:
            logger.error(f"Error in Atoma response: {str(e)}", exc_info=True)
            raise

    async def _get_groq_response(self, messages: str | list, model_type: ModelType, config: Dict) -> str:
        """Handle Groq API requests"""
        try:
            client = AsyncGroq(api_key=self.groq_api_key)
            formatted_messages = self._prepare_groq_messages(messages)
            
            model_config = self.model_configs[model_type]["default"].copy()
            model_config.update(config)
            
            logger.debug(f"Sending request to Groq {model_type.value}")
            
            completion = await client.chat.completions.create(
                model=model_type.value,
                messages=formatted_messages,
                **model_config
            )
            
            return completion.choices[0].message.content

        except Exception as e:
            logger.error(f"Error in Groq response: {str(e)}", exc_info=True)
            raise
        
    def _prepare_groq_messages(self, messages: str | list) -> list:
        """Format messages for Groq API"""
        if isinstance(messages, str):
            return [
                {
                    "role": "system",
                    "content": "You are a classifier. Given a user query, determine if it requires cryptocurrency price checking or internet search. Respond with EXACTLY one word: either 'CRYPTO' or 'SEARCH'. Nothing else."
                },
                {
                    "role": "user",
                    "content": messages
                }
            ]
        elif isinstance(messages, list):
            formatted_messages = []
            for msg in messages:
                if isinstance(msg, dict) and 'role' in msg and 'content' in msg:
                    if msg['role'] in ['system', 'user', 'assistant']:
                        formatted_messages.append(msg)
            return formatted_messages
        return messages