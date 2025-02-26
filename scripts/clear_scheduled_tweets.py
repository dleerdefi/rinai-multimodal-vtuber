import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from bson.objectid import ObjectId

# Update the path resolution for nested project structure
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from src.utils.logging_config import setup_logging
from src.db.mongo_manager import MongoManager
from src.db.enums import (
    ToolOperationState,
    OperationStatus,
    ContentType,
    ToolType,
    ScheduleState
)
import json

console = setup_logging()

def load_minimal_config():
    """Load minimal configuration needed for MongoDB connection"""
    try:
        # First try to use environment variable
        mongo_uri = os.getenv('MONGO_URI')
        if mongo_uri:
            console.print(f"[cyan]Using MongoDB URI from environment variable")
            return mongo_uri
            
        # Fall back to config file
        config_path = Path(__file__).parent.parent / 'src' / 'config' / 'config.json'
        console.print(f"[cyan]Loading config from: {config_path}")
        
        with open(config_path) as f:
            config_str = f.read()
            for key, value in os.environ.items():
                config_str = config_str.replace(f"${{{key}}}", value)
            config = json.loads(config_str)
            
        return config['mongodb']['uri']
    except Exception as e:
        console.print(f"[red]Error loading config: {e}")
        # Default to localhost if all else fails
        return "mongodb://localhost:27017"

async def clear_all_tool_related_collections():
    try:
        # Load environment variables and initialize MongoDB
        load_dotenv()
        mongo_uri = load_minimal_config()
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        
        console.print("[bold cyan]Starting cleanup of all tool-related collections...[/]")
        
        # Get counts before deletion for reporting
        before_counts = {
            "scheduled_operations": await db.scheduled_operations.count_documents({}),
            "tool_operations": await db.tool_operations.count_documents({}),
            "tool_items": await db.tool_items.count_documents({}),
            "tool_executions": await db.tool_executions.count_documents({})
        }
        
        console.print("[cyan]Current collection counts:[/]")
        for collection, count in before_counts.items():
            console.print(f"- {collection}: {count} documents")
        
        # Clear ALL collections except messages and context_configs
        results = {
            "scheduled_operations": await db.scheduled_operations.delete_many({}),
            "tool_operations": await db.tool_operations.delete_many({}),
            "tool_items": await db.tool_items.delete_many({}),
            "tool_executions": await db.tool_executions.delete_many({})
        }
        
        # Clear legacy collections if they exist
        legacy_results = {}
        if hasattr(db, 'tweets'):
            legacy_results["tweets"] = await db.tweets.delete_many({})
        if hasattr(db, 'tweet_schedules'):
            legacy_results["tweet_schedules"] = await db.tweet_schedules.delete_many({})
        
        # Verify collections are empty
        after_counts = {
            "scheduled_operations": await db.scheduled_operations.count_documents({}),
            "tool_operations": await db.tool_operations.count_documents({}),
            "tool_items": await db.tool_items.count_documents({}),
            "tool_executions": await db.tool_executions.count_documents({})
        }
        
        # Print deletion summary
        console.print("\n[bold cyan]Cleanup Summary:[/]")
        for collection, result in results.items():
            console.print(f"[green]- Deleted {result.deleted_count} documents from {collection}")
        
        # Print legacy deletion summary if applicable
        if legacy_results:
            console.print("\n[yellow]Legacy Collections Cleanup:[/]")
            for collection, result in legacy_results.items():
                console.print(f"[yellow]- Deleted {result.deleted_count} documents from {collection}")
        
        # Verify all collections are empty
        console.print("\n[bold cyan]Verification:[/]")
        all_empty = True
        for collection, count in after_counts.items():
            if count > 0:
                console.print(f"[red]Warning: {collection} still has {count} documents")
                all_empty = False
            else:
                console.print(f"[green]{collection} is empty")
        
        if all_empty:
            console.print("\n[bold green]All tool-related collections have been successfully cleared![/]")
        else:
            console.print("\n[bold yellow]Some collections may still contain documents. Check the warnings above.[/]")
        
        # Verify messages and context_configs are untouched
        messages_count = await db.messages.count_documents({})
        context_count = await db.context_configs.count_documents({})
        console.print("\n[bold cyan]Preserved Collections:[/]")
        console.print(f"[green]- rin.messages: {messages_count} documents (preserved)")
        console.print(f"[green]- rin.context_configs: {context_count} documents (preserved)")
        
    except Exception as e:
        console.print(f"[bold red]Error clearing collections: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await MongoManager.close()

async def clear_only_twitter_related_data():
    """Alternative function to clear only Twitter-related data"""
    try:
        # Load environment variables and initialize MongoDB
        load_dotenv()
        mongo_uri = load_minimal_config()
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        
        console.print("[bold cyan]Starting cleanup of Twitter-related data only...[/]")
        
        # Clear only Twitter-related data
        results = {
            "scheduled_operations": await db.scheduled_operations.delete_many({
                "$or": [
                    {"content_type": ContentType.TWEET.value},
                    {"tool_operation_id": {"$in": await get_twitter_operation_ids(db)}}
                ]
            }),
            "tool_operations": await db.tool_operations.delete_many({
                "$or": [
                    {"tool_type": ToolType.TWITTER.value},
                    {"metadata.content_type": ContentType.TWEET.value},
                    {"metadata.tool_type": ToolType.TWITTER.value}
                ]
            }),
            "tool_items": await db.tool_items.delete_many({
                "$or": [
                    {"content_type": ContentType.TWEET.value},
                    {"tool_operation_id": {"$in": await get_twitter_operation_ids(db)}}
                ]
            }),
            "tool_executions": await db.tool_executions.delete_many({
                "$or": [
                    {"tool_type": ToolType.TWITTER.value},
                    {"tool_operation_id": {"$in": await get_twitter_operation_ids(db)}}
                ]
            })
        }
        
        # Print deletion summary
        console.print("\n[bold cyan]Twitter Data Cleanup Summary:[/]")
        for collection, result in results.items():
            console.print(f"[green]- Deleted {result.deleted_count} Twitter-related documents from {collection}")
        
    except Exception as e:
        console.print(f"[bold red]Error clearing Twitter data: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await MongoManager.close()

async def get_twitter_operation_ids(db):
    """Helper function to get all Twitter operation IDs"""
    operations = await db.tool_operations.find(
        {"tool_type": ToolType.TWITTER.value}
    ).to_list(None)
    return [str(op["_id"]) for op in operations]

if __name__ == "__main__":
    try:
        # Parse command line arguments
        import argparse
        parser = argparse.ArgumentParser(description='Clear tool-related collections from MongoDB')
        parser.add_argument('--twitter-only', action='store_true', help='Clear only Twitter-related data')
        args = parser.parse_args()
        
        if args.twitter_only:
            asyncio.run(clear_only_twitter_related_data())
            console.print("[bold green]Successfully cleared Twitter-related data![/]")
        else:
            asyncio.run(clear_all_tool_related_collections())
            console.print("[bold green]Successfully cleared all tool-related collections![/]")
    except KeyboardInterrupt:
        console.print("[yellow]Operation cancelled by user")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {e}")
    finally:
        console.print("[green]Exiting...") 