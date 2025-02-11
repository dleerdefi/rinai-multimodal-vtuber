import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Update the path resolution for nested project structure
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)  # Points directly to inner rinai-multimodal-vtuber
sys.path.append(project_root)

# Import after sys.path modification
from src.utils.logging_config import setup_logging
from src.db.mongo_manager import MongoManager
import json

# Set up logging
console = setup_logging()

def load_minimal_config():
    """Load minimal configuration needed for MongoDB connection"""
    try:
        # Updated path to point to src/config/config.json
        config_path = Path(__file__).parent.parent / 'src' / 'config' / 'config.json'
        console.print(f"[cyan]Loading config from: {config_path}")
        
        with open(config_path) as f:
            config_str = f.read()
            
            # Replace environment variables
            for key, value in os.environ.items():
                config_str = config_str.replace(f"${{{key}}}", value)
            
            config = json.loads(config_str)
            
        return config['mongodb']['uri']
        
    except Exception as e:
        console.print(f"[red]Error loading config: {e}")
        raise

async def clear_all_scheduled_tweets():
    try:
        # Load environment variables
        load_dotenv()
        mongo_uri = load_minimal_config()
            
        # Initialize MongoDB connection
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        
        # Get all schedules with specific statuses
        schedule_cursor = db.tweet_schedules.find({
            "status": {"$in": ["collecting_approval", "scheduled"]}
        })
        schedules = await schedule_cursor.to_list(length=None)
        
        if not schedules:
            console.print("[yellow]No tweet schedules found with status 'collecting_approval' or 'scheduled'")
            return
        
        schedule_ids = [str(schedule['_id']) for schedule in schedules]
        
        # Delete all associated tweets (both pending and scheduled)
        for schedule_id in schedule_ids:
            tweet_result = await db.tweets.delete_many({
                "schedule_id": schedule_id,
                "status": {"$in": ["pending", "scheduled"]}
            })
            console.print(f"[green]Deleted {tweet_result.deleted_count} tweets for schedule {schedule_id}")
        
        # Delete the schedules
        schedule_result = await db.tweet_schedules.delete_many({
            "status": {"$in": ["collecting_approval", "scheduled"]}
        })
        
        console.print("\n[bold cyan]Operation Summary:[/]")
        console.print(f"[green]- Deleted {schedule_result.deleted_count} tweet schedules")
        console.print("\n[cyan]Deleted Schedule IDs:[/]")
        for schedule_id in schedule_ids:
            console.print(f"[green]- {schedule_id}")
            
    except Exception as e:
        console.print(f"[bold red]Error clearing scheduled tweets: {e}")
        raise
    finally:
        await MongoManager.close()

if __name__ == "__main__":
    try:
        asyncio.run(clear_all_scheduled_tweets())
        console.print("[bold green]Successfully cleared all scheduled tweets![/]")
    except KeyboardInterrupt:
        console.print("[yellow]Operation cancelled by user")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {e}")
    finally:
        console.print("[green]Exiting...") 