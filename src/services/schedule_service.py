import asyncio
import logging
from datetime import datetime, UTC
from motor.motor_asyncio import AsyncIOMotorClient
from src.clients.twitter_client import TwitterAgentClient
from src.db.db_schema import TweetStatus, RinDB
from src.managers.tool_state_manager import ToolOperationState
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)

class ScheduleService:
    def __init__(self, mongo_uri: str):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = RinDB(self.mongo_client)  # Use RinDB class instead of raw db
        self.twitter_client = TwitterAgentClient()
        self.running = False
        self._task = None

    async def start(self):
        """Start the scheduling service"""
        if self.running:
            return
        
        await self.db.initialize()  # Initialize RinDB
        self.running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("Schedule service started")

    async def _schedule_loop(self):
        """Main scheduling loop that checks for and executes due tweets"""
        while self.running:
            try:
                # Get tweets ready for execution using RinDB method
                due_tweets = await self.db.get_scheduled_tweets_for_execution()
                
                for tweet in due_tweets:
                    try:
                        logger.info(f"Processing scheduled tweet {tweet['_id']}")
                        
                        # Use correct parameters for send_tweet
                        result = await self.twitter_client.send_tweet(
                            content=tweet['content'],
                            params={
                                "account_id": tweet.get('twitter_api_params', {}).get('account_id', 'default'),
                                "media_files": tweet.get('twitter_api_params', {}).get('media_files'),
                                "poll_options": tweet.get('twitter_api_params', {}).get('poll_options'),
                                "poll_duration": tweet.get('twitter_api_params', {}).get('poll_duration')
                            }
                        )
                        
                        if result and result.get('success'):
                            # Update tweet status using RinDB method
                            await self.db.update_tweet_status(
                                tweet_id=str(tweet['_id']),
                                status=TweetStatus.POSTED,
                                twitter_response=result
                            )
                            logger.info(f"Successfully posted tweet {tweet['_id']}")
                            
                            # Check if schedule is complete
                            await self._check_schedule_completion(tweet['schedule_id'])
                        else:
                            error_msg = result.get('error', 'Unknown error') if result else "No response from Twitter client"
                            logger.error(f"Failed to post tweet {tweet['_id']}: {error_msg}")
                            await self.db.update_tweet_status(
                                tweet_id=str(tweet['_id']),
                                status=TweetStatus.FAILED,
                                error=error_msg
                            )
                    
                    except Exception as e:
                        logger.error(f"Error processing tweet {tweet['_id']}: {e}")
                        continue
                
                # Wait before next check
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _check_schedule_completion(self, schedule_id: str):
        """Check if all tweets in a schedule are posted"""
        try:
            # Get all tweets for this schedule
            schedule_tweets = await self.db.get_tweets_by_schedule(schedule_id)
            
            # Count total and posted tweets
            total = len(schedule_tweets)
            posted = sum(1 for t in schedule_tweets if t['status'] == TweetStatus.POSTED.value)

            if total == posted:
                # All tweets posted, update schedule status
                await self.db.update_tweet_schedule(
                    schedule_id=schedule_id,
                    status="completed"
                )
                logger.info(f"Schedule {schedule_id} completed")

        except Exception as e:
            logger.error(f"Error checking schedule completion: {e}")

    async def stop(self):
        """Stop the scheduling service"""
        if not self.running:
            return
            
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Schedule service stopped") 