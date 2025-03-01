import asyncio
import logging
import os
import sys
import json
from datetime import datetime, UTC
from dotenv import load_dotenv

# Add the src directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
SOLVER_BUS_URL = os.getenv("SOLVER_BUS_URL", "https://solver-bus.near.org")

class SolverQuoteTest:
    """Test class for solver quotes functionality"""
    
    def __init__(self):
        """Initialize test parameters"""
        # Import here to avoid import errors
        from clients.solver_bus_client import SolverBusClient
        from clients.near_intents_client.intents_client import (
            get_near_account, 
            IntentRequest,
            fetch_options,
            select_best_option
        )
        from clients.near_intents_client.config import (
            get_token_by_symbol,
            to_asset_id,
            to_decimals,
            from_decimals
        )
        
        # Store the imported functions
        self.get_token_by_symbol = get_token_by_symbol
        self.to_asset_id = to_asset_id
        self.to_decimals = to_decimals
        self.from_decimals = from_decimals
        self.IntentRequest = IntentRequest
        self.fetch_options = fetch_options
        self.select_best_option = select_best_option
        
        # Initialize clients
        self.solver_bus_client = SolverBusClient(SOLVER_BUS_URL)
        self.near_account = get_near_account()
        
        # Test parameters
        self.from_token = "NEAR"
        self.to_token = "USDC"
        self.from_amount = 0.1  # Small amount for testing
        self.check_interval = 10  # Check every 10 seconds
        self.target_price = 1.5  # Target price to consider executing
        
    async def test_recurring_quotes(self, iterations=5):
        """Test recurring quote fetching"""
        logger.info(f"Starting recurring quote test for {iterations} iterations")
        logger.info(f"Converting {self.from_amount} {self.from_token} to {self.to_token}")
        logger.info(f"Target price: {self.target_price} {self.to_token}/{self.from_token}")
        
        # Track best price seen
        best_price_seen = 0
        
        # Fetch quotes multiple times with a delay
        for i in range(iterations):
            logger.info(f"\nQuote fetch iteration {i+1}/{iterations}")
            
            try:
                # Create intent request using the proper class
                request = self.IntentRequest()
                request.asset_in(self.from_token, self.from_amount)
                request.asset_out(self.to_token, chain="eth")
                
                logger.info(f"Getting quotes for {self.from_amount} {self.from_token} to {self.to_token}")
                logger.info(f"Asset IDs: {request.asset_in['asset']} -> {request.asset_out['asset']}")
                
                # Get quotes using the fetch_options function that works
                solver_quotes = self.fetch_options(request)
                
                logger.info(f"Received {len(solver_quotes)} quotes")
                
                if solver_quotes:
                    # Find best quote using the existing function
                    best_option = self.select_best_option(solver_quotes)
                    
                    if best_option:
                        # Calculate current price
                        from_token_info = self.get_token_by_symbol(self.from_token)
                        to_token_info = self.get_token_by_symbol(self.to_token)
                        
                        from_decimals_val = from_token_info.get('decimals', 24) if from_token_info else 24
                        to_decimals_val = to_token_info.get('decimals', 6) if to_token_info else 6
                        
                        human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals_val)
                        human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals_val)
                        
                        current_price = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                        
                        logger.info(f"Best quote details:")
                        logger.info(f"Current price: {current_price} {self.to_token}/{self.from_token}")
                        logger.info(f"You would receive: {human_amount_out} {self.to_token} for {human_amount_in} {self.from_token}")
                        logger.info(f"Quote hash: {best_option.get('quote_hash')}")
                        logger.info(f"Solver ID: {best_option.get('solver_id')}")
                        
                        # Save the quote to a file for reference
                        with open(f"quote_result_{i}.json", "w") as f:
                            json.dump(best_option, f, indent=2)
                            logger.info(f"Saved quote to quote_result_{i}.json")
                            
                        # Update best price seen
                        if current_price > best_price_seen:
                            best_price_seen = current_price
                            logger.info(f"New best price found: {best_price_seen}")
                            
                        # Check if target price is met
                        if current_price >= self.target_price:
                            logger.info(f"TARGET PRICE MET! Current price ({current_price}) >= Target price ({self.target_price})")
                            logger.info("In a real scenario, we would execute the swap now")
                        else:
                            logger.info(f"Target price not met. Current: {current_price}, Target: {self.target_price}")
                else:
                    logger.warning("No quotes available")
                    
            except Exception as e:
                logger.error(f"Error getting quotes: {e}", exc_info=True)
            
            # Wait before next fetch (except for the last iteration)
            if i < iterations - 1:
                logger.info(f"Waiting {self.check_interval} seconds before next check...")
                await asyncio.sleep(self.check_interval)
        
        # Summary
        logger.info("\n===== TEST SUMMARY =====")
        logger.info(f"Completed {iterations} quote checks")
        logger.info(f"Best price seen: {best_price_seen} {self.to_token}/{self.from_token}")
        logger.info(f"Target price: {self.target_price} {self.to_token}/{self.from_token}")
        logger.info(f"Target price met: {'Yes' if best_price_seen >= self.target_price else 'No'}")
        
        return best_price_seen

    async def test_with_direct_swap(self, iterations=5):
        """Test using the intent_swap function directly"""
        logger.info(f"Starting direct swap test for {iterations} iterations")
        
        from clients.near_intents_client.intents_client import intent_swap
        
        for i in range(iterations):
            logger.info(f"\nDirect swap test iteration {i+1}/{iterations}")
            
            try:
                # Don't actually execute the swap, just get the quote
                logger.info(f"Getting quote for {self.from_amount} {self.from_token} to {self.to_token}")
                
                # Create intent request using the proper class
                request = self.IntentRequest()
                request.asset_in(self.from_token, self.from_amount)
                request.asset_out(self.to_token, chain="eth")
                
                # Get quotes using the fetch_options function that works
                solver_quotes = self.fetch_options(request)
                
                if solver_quotes:
                    best_option = self.select_best_option(solver_quotes)
                    
                    if best_option:
                        # Calculate current price
                        from_token_info = self.get_token_by_symbol(self.from_token)
                        to_token_info = self.get_token_by_symbol(self.to_token)
                        
                        from_decimals_val = from_token_info.get('decimals', 24) if from_token_info else 24
                        to_decimals_val = to_token_info.get('decimals', 6) if to_token_info else 6
                        
                        human_amount_in = float(best_option['amount_in']) / (10 ** from_decimals_val)
                        human_amount_out = float(best_option['amount_out']) / (10 ** to_decimals_val)
                        
                        current_price = human_amount_out / human_amount_in if human_amount_in > 0 else 0
                        
                        logger.info(f"Best quote details:")
                        logger.info(f"Current price: {current_price} {self.to_token}/{self.from_token}")
                        logger.info(f"You would receive: {human_amount_out} {self.to_token} for {human_amount_in} {self.from_token}")
                        
                        # Save the quote to a file for reference
                        with open(f"direct_quote_result_{i}.json", "w") as f:
                            json.dump(best_option, f, indent=2)
                            logger.info(f"Saved quote to direct_quote_result_{i}.json")
                else:
                    logger.warning("No quotes available")
            
            except Exception as e:
                logger.error(f"Error in direct swap test: {e}", exc_info=True)
            
            # Wait before next fetch
            if i < iterations - 1:
                logger.info(f"Waiting {self.check_interval} seconds before next check...")
                await asyncio.sleep(self.check_interval)
        
        logger.info("\n===== DIRECT SWAP TEST COMPLETE =====")
        return True

async def main():
    """Main function to run the test"""
    test = SolverQuoteTest()
    
    # Choose which test to run
    test_type = input("Choose test type (1 for recurring quotes, 2 for direct swap test): ")
    
    if test_type == "2":
        await test.test_with_direct_swap(iterations=3)
    else:
        await test.test_recurring_quotes(iterations=5)

if __name__ == "__main__":
    asyncio.run(main()) 