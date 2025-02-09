import asyncio
import logging
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from src.clients.twitter_client import TwitterAgentClient
from src.db.db_schema import TweetStatus, ToolOperationState

logger = logging.getLogger(__name__)

class ScheduleService:
    def __init__(self, mongo_uri: str):
        self.mongo_client = AsyncIOMotorClient(mongo_uri)
        self.db = self.mongo_client.rinai
        self.twitter_client = TwitterAgentClient()
        self.running = False
        self._task = None

    async def start(self):
        """Start the scheduling service"""
        self.running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("Schedule service started")

    async def stop(self):
        """Stop the scheduling service"""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Schedule service stopped")

    async def _schedule_loop(self):
        """Main scheduling loop"""
        while self.running:
            try:
                # Find pending tweets that are due
                now = datetime.utcnow()
                cursor = self.db.tweets.find({
                    'status': TweetStatus.SCHEDULED,
                    'scheduled_time': {'$lte': now},
                    'retry_count': {'$lt': 3}  # Limit retries
                })

                async for tweet in cursor:
                    try:
                        # Check if part of active tool operation
                        operation = await self.db.get_tool_operation_state(tweet['session_id'])
                        if operation and operation['state'] != ToolOperationState.COMPLETED.value:
                            logger.info(f"Skipping tweet {tweet['_id']} - active operation")
                            continue

                        # Post the tweet
                        result = await self.twitter_client.send_tweet(
                            message=tweet['content'],
                            account_id=tweet.get('account_id', 'default')
                        )

                        # Update status
                        await self.db.update_tweet_status(
                            tweet['_id'],
                            TweetStatus.POSTED,
                            twitter_response=result
                        )
                        logger.info(f"Posted scheduled tweet: {tweet['_id']}")

                    except Exception as e:
                        logger.error(f"Error posting tweet {tweet['_id']}: {e}")
                        await self.db.update_tweet_status(
                            tweet['_id'],
                            TweetStatus.FAILED,
                            error=str(e)
                        )

            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")

            await asyncio.sleep(60)  # Check every minute 