import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Update the path resolution for nested project structure
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from src.utils.logging_config import setup_logging
from src.db.mongo_manager import MongoManager
from src.db.db_schema import (
    ToolOperationState,
    OperationStatus,
    ContentType,
    ToolType
)
import json

console = setup_logging()

def load_minimal_config():
    """Load minimal configuration needed for MongoDB connection"""
    try:
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
        raise

async def clear_all_scheduled_operations():
    try:
        # Load environment variables and initialize MongoDB
        load_dotenv()
        mongo_uri = load_minimal_config()
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        
        # Diagnostic: Print a sample tool operation to see structure
        sample_op = await db.tool_operations.find_one({})
        if sample_op:
            console.print("[cyan]Sample tool operation structure:[/]")
            console.print(sample_op)
        
        # Clear ALL scheduled operations related to tweets
        scheduled_ops_result = await db.scheduled_operations.delete_many({
            "content_type": ContentType.TWEET.value
        })
        
        # Clear ALL tool operations related to Twitter with expanded query
        tool_ops_result = await db.tool_operations.delete_many({
            "$or": [
                {"metadata.content_type": ContentType.TWEET.value},
                {"metadata.tool_type": ToolType.TWITTER.value},
                {"tool_type": ToolType.TWITTER.value},  # Direct field match
                {"content_type": ContentType.TWEET.value},  # Direct field match
                {"input_data.content_type": ContentType.TWEET.value},
                {"input_data.tool_type": ToolType.TWITTER.value},
                {"output_data.content_type": ContentType.TWEET.value},
                {"output_data.tool_type": ToolType.TWITTER.value}
            ]
        })
        
        # Clear ALL tool items related to tweets
        tool_items_result = await db.tool_items.delete_many({
            "$or": [
                {"content_type": ContentType.TWEET.value},
                {"metadata.content_type": ContentType.TWEET.value},
                {"metadata.tool_type": ToolType.TWITTER.value}
            ]
        })
        
        # Clear ALL tool executions related to Twitter
        tool_exec_result = await db.tool_executions.delete_many({
            "$or": [
                {"metadata.content_type": ContentType.TWEET.value},
                {"metadata.tool_type": ToolType.TWITTER.value}
            ]
        })
        
        # Clear legacy collections
        legacy_results = {
            "tweets": await db.tweets.delete_many({}),  # Clear all legacy tweets
            "tweet_schedules": await db.tweet_schedules.delete_many({})  # Clear all legacy schedules
        }
        
        # Print current collection counts for verification
        counts = {
            "scheduled_operations": await db.scheduled_operations.count_documents({"content_type": ContentType.TWEET.value}),
            "tool_operations": await db.tool_operations.count_documents({
                "$or": [
                    {"metadata.content_type": ContentType.TWEET.value},
                    {"metadata.tool_type": ToolType.TWITTER.value}
                ]
            }),
            "tool_items": await db.tool_items.count_documents({
                "content_type": ContentType.TWEET.value
            }),
            "tool_executions": await db.tool_executions.count_documents({
                "metadata.tool_type": ToolType.TWITTER.value
            })
        }
        
        console.print("\n[bold cyan]Cleanup Summary:[/]")
        console.print(f"[green]- Deleted {scheduled_ops_result.deleted_count} scheduled operations")
        console.print(f"[green]- Deleted {tool_ops_result.deleted_count} tool operations")
        console.print(f"[green]- Deleted {tool_items_result.deleted_count} tool items")
        console.print(f"[green]- Deleted {tool_exec_result.deleted_count} tool executions")
        
        console.print("\n[bold cyan]Remaining Items:[/]")
        for collection, count in counts.items():
            if count > 0:
                console.print(f"[yellow]Warning: {count} items remain in {collection}")
            else:
                console.print(f"[green]{collection} is empty")
        
        if any(result.deleted_count > 0 for result in legacy_results.values()):
            console.print("\n[yellow]Legacy Collections Cleanup:[/]")
            for collection, result in legacy_results.items():
                if result.deleted_count > 0:
                    console.print(f"[yellow]- Deleted {result.deleted_count} items from {collection}")
        
        # Additional diagnostic after deletion
        remaining_ops = await db.tool_operations.find({}).to_list(None)
        if remaining_ops:
            console.print(f"\n[yellow]Sample of remaining operations ({len(remaining_ops)}):[/]")
            for op in remaining_ops[:3]:  # Show first 3 as samples
                console.print(f"ID: {op['_id']}")
                console.print(f"Type: {op.get('tool_type')}")
                console.print(f"Content: {op.get('content_type')}")
                console.print(f"Metadata: {op.get('metadata', {})}")
                console.print("---")
        
    except Exception as e:
        console.print(f"[bold red]Error clearing scheduled operations: {e}")
        raise
    finally:
        await MongoManager.close()

if __name__ == "__main__":
    try:
        asyncio.run(clear_all_scheduled_operations())
        console.print("[bold green]Successfully cleared all scheduled operations![/]")
    except KeyboardInterrupt:
        console.print("[yellow]Operation cancelled by user")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {e}")
    finally:
        console.print("[green]Exiting...") 