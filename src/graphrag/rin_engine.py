import asyncio
import platform

if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from voyageai import Client
from neo4j import GraphDatabase
import os
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class RinResponseEnricher:
    def __init__(self, uri=None, username=None, password=None):
        """Initialize response enricher with Neo4j connection."""
        # Try constructor params first, then environment variables
        self.uri = uri or os.getenv("NEO4J_URI")
        self.username = username or os.getenv("NEO4J_USERNAME")
        self.password = password or os.getenv("NEO4J_PASSWORD")
        
        # Log configuration status
        if all([self.uri, self.username, self.password]):
            logger.info("Neo4j credentials configured successfully")
        else:
            logger.warning("Neo4j credentials not fully configured")
        self.driver = None
        self.voyage = None
        
    async def initialize(self):
        """Initialize Neo4j and Voyage AI connections"""
        try:
            # Initialize Neo4j driver
            if all([self.uri, self.username, self.password]):
                self.driver = GraphDatabase.driver(
                    self.uri,
                    auth=(self.username, self.password)
                )
                # Test connection
                with self.driver.session() as session:
                    session.run("RETURN 1")
                logger.info("Successfully connected to Neo4j")
            else:
                logger.warning("Neo4j credentials not provided, GraphRAG will be disabled")
                return False
            
            # Initialize Voyage AI
            voyage_key = os.getenv("VOYAGE_API_KEY")
            if voyage_key:
                self.voyage = Client(api_key=voyage_key)
                logger.info("Successfully initialized Voyage AI client")
            else:
                logger.warning("Voyage AI API key not found, GraphRAG will be disabled")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize GraphRAG components: {e}")
            self.driver = None
            self.voyage = None
            return False

    def get_context(self, user_query: str, threshold: float = 0.70) -> List[Dict[str, Any]]:
        """Get similar context from Neo4j"""
        try:
            if not self.voyage:
                logger.warning("Voyage AI not initialized")
                return []
            
            # Generate embedding
            query_embedding = self.voyage.embed(
                texts=[user_query],
                model="voyage-3-large"
            ).embeddings[0]
            
            with self.driver.session() as session:
                # Execute Neo4j query
                candidates = session.run(
                    """
                    MATCH (user:User)-[:SENT]->(user_msg:Message)-[:NEXT]->(assistant_msg:Message)
                    WHERE user_msg.voyage_embedding IS NOT NULL
                    AND user_msg.role = 'user'
                    AND assistant_msg.role = 'assistant'
                    
                    WITH user_msg, assistant_msg,
                         vector.similarity.cosine(user_msg.voyage_embedding, $embedding) AS semantic_score
                    WHERE semantic_score > $threshold
                    
                    // Get conversation context and filter sentiment
                    MATCH (user_msg)-[:IN_CONVERSATION]->(conv:Conversation)
                    WHERE conv.sentiment <> 'negative'
                    
                    RETURN 
                        assistant_msg.content as response,
                        semantic_score as score
                    ORDER BY score DESC
                    LIMIT 3
                    """,
                    embedding=query_embedding,
                    threshold=threshold
                ).data()
                
                return candidates if candidates else []
            
        except Exception as e:
            logger.error(f"Error getting context: {e}", exc_info=True)
            return []

    async def enrich_response(self, user_query: str) -> str:
        """Async method to get enriched response"""
        try:
            if not self.driver or not self.voyage:
                logger.warning("GraphRAG components not initialized")
                return "Consider this a fresh conversation."
            
            # Get similar contexts
            similar_contexts = self.get_context(user_query)
            
            if not similar_contexts:
                logger.info("No similar contexts found")
                return "Consider this a fresh conversation."
            
            # Always return a string response
            best_match = similar_contexts[0]
            if isinstance(best_match, dict):
                return best_match.get('response', "Consider this a fresh conversation.")
            elif isinstance(best_match, str):
                return best_match
            else:
                logger.warning(f"Unexpected response format: {type(best_match)}")
                return "Consider this a fresh conversation."
            
        except Exception as e:
            logger.error(f"Failed to enrich response: {e}", exc_info=True)
            return "Consider this a fresh conversation."

    def analyze_query_intent(self, query: str) -> set:
        """Determine query type and appropriate sentiment targets"""
        intents = {
            'greeting': ['hey', 'whats up', 'yo', 'hai', 'hello', 'hi'],
            'personal': ['yourself', 'about you', 'share', 'sad', 'lonely', 'depressed', 'who are'],
            'nsfw': ['ass', 'porn', 'fuck', 'cum', 'horny', 'sex'],
            'interests': ['favorite', 'like', 'enjoy', 'crypto', 'art', 'anime', 'gaming', 'internet culture']
        }
        
        query_lower = query.lower()
        matched_intents = set()
        
        # Match intents based on keywords
        for intent, keywords in intents.items():
            if any(word in query_lower for word in keywords):
                matched_intents.add(intent)
        
        print(f"Query: {query}")
        print(f"Detected intents: {matched_intents}")
        
        return matched_intents or {'general'}  # Return 'general' if no specific intents found

    async def cleanup(self):
        """Cleanup Neo4j connection and other resources"""
        try:
            if self.driver:
                self.driver.close()  # Neo4j driver's close is synchronous
                self.driver = None
            if hasattr(self, 'voyage') and self.voyage:
                # Clean up Voyage client if it has any cleanup needs
                if hasattr(self.voyage, 'close'):
                    await self.voyage.close()
                self.voyage = None
            logger.info("Successfully cleaned up GraphRAG resources")
        except Exception as e:
            logger.error(f"Error during GraphRAG cleanup: {e}")

def main():
    enricher = RinResponseEnricher()
    
    test_queries = [
        "Tell me about yourself."
    ]
    
    for query in test_queries:
        print(f"\nTesting query: {query}")
        print("=" * 80)
        
        context = enricher.enrich_response(query)
        print("\nLLM Guidance:")
        print(context)
        print("-" * 40)

if __name__ == "__main__":
    main() 