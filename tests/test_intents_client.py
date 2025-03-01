import pytest

import os
import sys
from dotenv import load_dotenv
from near_api.account import Account
from near_api.signer import KeyPair, Signer
from near_api.providers import JsonProvider
from decimal import Decimal
import time
import random
import base64
import json
import base58
import logging

# Add the src directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
from clients.near_Intents_client.intents_client import (
    intent_deposit, 
    smart_withdraw,
    intent_swap,
    get_intent_balance,
    wrap_near,
    publish_intent,
    Quote,
    Intent,
    PublishIntent,
    get_future_deadline,
    sign_quote,
    MAX_GAS,
    unwrap_near
)
from clients.near_Intents_client import config  # Import the config module
from clients.near_Intents_client.config import (
    get_token_by_symbol,
    get_defuse_asset_id,
    to_decimals,
    from_decimals
)

# Configure logger with proper format and level
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Simplified format to just show the message
)
logger = logging.getLogger(__name__)

#RUN THIS TEST WITH: pytest tests/test_intents_client.py

# Load environment variables
load_dotenv()

# Simple fixture for account setup - reuses your existing initialization
@pytest.fixture
def account():
    account_id = os.getenv('NEAR_ACCOUNT_ID')
    private_key = os.getenv('NEAR_PRIVATE_KEY')
    provider = JsonProvider(os.getenv('NEAR_RPC_URL'))
    key_pair = KeyPair(private_key)
    signer = Signer(account_id, key_pair)
    return Account(provider, signer)

@pytest.fixture
def setup_account(account):
    """Setup account with registered public key if needed"""
    try:
        print("\nChecking public key registration...")
        public_key = "ed25519:" + base58.b58encode(account.signer.public_key).decode('utf-8')
        
        # Check if already registered
        result = account.view_function(
            "intents.near",
            "has_public_key",
            {"public_key": public_key}
        )
        
        if not result['result']:
            print("Public key not registered, registering now...")
            register_intent_public_key(account)
            time.sleep(2)  # Wait for registration
        else:
            print("Public key already registered")
            
        return account
    except Exception as e:
        pytest.fail(f"Failed to check/register public key: {str(e)}")

def test_near_deposit_and_withdraw(account):
    """Test depositing and withdrawing NEAR"""
    
    # Check initial balance
    initial_balance = get_intent_balance(account, "NEAR")
    account_state = account.provider.get_account(account.account_id)
    print(f"Initial NEAR balance in intents account: {initial_balance}")
    print(f"Initial NEAR balance: {from_decimals(account_state['amount'], 'NEAR')}")
    
    # Deposit 0.01 NEAR
    deposit_amount = 0.01
    print(f"\nDepositing {deposit_amount} NEAR...")
    try:
        # First wrap the NEAR
        wrap_result = wrap_near(account, deposit_amount)
        time.sleep(3)
        
        # Then deposit
        result = intent_deposit(account, "NEAR", deposit_amount)
        print("Deposit successful:", result)
    except Exception as e:
        print("Deposit failed:", str(e))
        return
    
    # Check balance after deposit
    new_balance = get_intent_balance(account, "NEAR")
    account_state = account.provider.get_account(account.account_id)
    print(f"NEAR balance after deposit in Intents account: {new_balance}")
    print(f"NEAR account balance after deposit in NEAR account: {from_decimals(account_state['amount'], 'NEAR')}")
    
    # Withdraw 0.005 NEAR using smart_withdraw
    withdraw_amount = 0.005
    print(f"\nWithdrawing {withdraw_amount} NEAR...")
    try:
        result = smart_withdraw(
            account=account,
            token="NEAR",
            amount=withdraw_amount,
            destination_chain="near"  # Optional, defaults to "near"
        )
        print("Withdrawal successful:", result)
    except Exception as e:
        print("Withdrawal failed:", str(e))
    
    # Check final balance
    time.sleep(3)
    final_balance = get_intent_balance(account, "NEAR")
    print(f"Final NEAR balance in intents: {final_balance}")

def get_balances(account):
    """Get NEAR, USDC, and SOL balances across chains"""
    return {
        "NEAR": get_intent_balance(account, "NEAR"),
        "USDC": {
            "eth": get_intent_balance(account, "USDC", chain="eth"),
            "near": get_intent_balance(account, "USDC", chain="near")
        },
        "SOL": get_intent_balance(account, "SOL", chain="solana")
    }

def test_near_usdc_swap(account):
    """Test getting quotes and swapping NEAR to USDC"""
    # Log initial balances
    initial_usdc = get_intent_balance(account, "USDC", chain="eth")
    initial_near = get_intent_balance(account, "NEAR")
    print(f"\nInitial Balances:")  # Using print for now
    print(f"NEAR: {initial_near}")
    print(f"USDC (ETH): {initial_usdc}")
    
    # Execute deposit and swap
    deposit_amount = 0.1
    print(f"\nDepositing {deposit_amount} NEAR...")
    wrap_result = wrap_near(account, deposit_amount)
    time.sleep(3)
    result = intent_deposit(account, "NEAR", deposit_amount)
    
    # Execute swap and log response
    print(f"\nExecuting NEAR to USDC swap...")
    swap_result = intent_swap(account, "NEAR", deposit_amount, "USDC", chain_out="eth")
    print("\nSwap Result Details:")
    print(json.dumps(swap_result, indent=2))
    time.sleep(3)
    
    # Calculate and log the swap amount
    if 'amount_out' not in swap_result:
        pytest.fail("Swap failed - no amount_out in response")
    swap_amount = config.from_decimals(swap_result['amount_out'], 'USDC')
    print(f"Successfully swapped {deposit_amount} NEAR for {swap_amount} USDC")
    
    # Execute withdrawal
    print(f"\nWithdrawing {swap_amount} USDC to NEAR chain...")
    withdrawal_result = smart_withdraw(
        account=account,
        token="USDC",
        amount=swap_amount,
        destination_chain="near",
        destination_address=account.account_id
    )
    print("Withdrawal complete")

def test_near_sol_swap(account):
    """Test swapping NEAR to SOL and withdrawing to Solana wallet"""
    def get_balances():
        """Get NEAR and SOL balances"""
        return {
            "NEAR": get_intent_balance(account, "NEAR"),
            "SOL": get_intent_balance(account, "SOL", chain="solana")
        }

    initial_balances = get_balances()
    print("\nInitial Balances:", json.dumps(initial_balances, indent=2))
    
    # Deposit NEAR
    deposit_amount = 0.4
    print(f"\nDepositing {deposit_amount} NEAR...")
    try:
        wrap_result = wrap_near(account, deposit_amount)
        time.sleep(3)
        result = intent_deposit(account, "NEAR", deposit_amount)
        print("Deposit successful:", result)
    except Exception as e:
        print("Deposit failed:", str(e))
        return
    
    # Execute swap
    try:
        print("\nExecuting NEAR to SOL swap...")
        swap_result = intent_swap(account, "NEAR", deposit_amount, "SOL", chain_out="solana")
        print("\nSwap Result Details:")
        print(json.dumps(swap_result, indent=2))
        time.sleep(3)
        
        # Get post-swap balances
        print("\nBalances After Swap:")
        post_swap_balances = get_balances()
        print(json.dumps(post_swap_balances, indent=2))
        
        # Use config's decimal conversion
        swap_amount = config.from_decimals(swap_result['amount_out'], 'SOL')
        print(f"\nAttempting to withdraw {swap_amount} SOL")
        print(f"Current SOL balance: {post_swap_balances['SOL']}")
        
        # Use smart_withdraw for SOL withdrawal to Solana wallet
        try:
            print(f"\nWithdrawing {swap_amount} SOL to Solana wallet...")
            withdrawal_result = smart_withdraw(
                account=account,
                token="SOL",
                amount=swap_amount,
                destination_chain="solana",     # Token lives on Solana chain
                destination_address=os.getenv('SOLANA_ACCOUNT_ID')  # Send to Solana wallet
            )
            print("\nWithdrawal Result:")
            print(json.dumps(withdrawal_result, indent=2))
            time.sleep(3)
            
            final_balances = get_balances()
            print("\nFinal Balances:")
            print(json.dumps(final_balances, indent=2))
            
        except Exception as e:
            print(f"\nWithdrawal Error: {str(e)}")
            print(f"Current SOL balance: {get_intent_balance(account, 'SOL', chain='solana')}")
        
    except Exception as e:
        print("Swap failed:", str(e))
        return
    
    return {
        "initial_balances": initial_balances,
        "final_balances": final_balances,
        "amount_swapped": deposit_amount,
        "amount_received": swap_amount
    }

def test_usdc_near_swap(account):
    """Test swapping USDC to NEAR and withdrawing"""
    # Log initial balances
    initial_usdc = get_intent_balance(account, "USDC", chain="near")
    initial_near = get_intent_balance(account, "NEAR")
    print(f"\nInitial Balances:")
    print(f"NEAR: {initial_near}")
    print(f"USDC (NEAR): {initial_usdc}")
    
    # Execute deposit and swap
    deposit_amount = 0.3
    print(f"\nDepositing {deposit_amount} USDC...")
    result = intent_deposit(account, "USDC", deposit_amount)
    time.sleep(3)
    
    # Execute swap and log response
    print(f"\nExecuting USDC to NEAR swap...")
    swap_result = intent_swap(account, "USDC", deposit_amount, "NEAR", chain_out="near")
    print("\nSwap Result Details:")
    print(json.dumps(swap_result, indent=2))
    time.sleep(3)
    
    # Calculate and log the swap amount
    if 'amount_out' not in swap_result:
        pytest.fail("Swap failed - no amount_out in response")
    swap_amount = config.from_decimals(swap_result['amount_out'], 'NEAR')
    print(f"Successfully swapped {deposit_amount} USDC for {swap_amount} NEAR")
    
    # Execute withdrawal
    print(f"\nWithdrawing {swap_amount} NEAR to account...")
    withdrawal_result = smart_withdraw(
        account=account,
        token="NEAR",
        amount=swap_amount,
        destination_chain="near",
        destination_address=account.account_id
    )
    print("Withdrawal complete")
    
    # Unwrap the received wNEAR
    time.sleep(3)  # Wait for withdrawal to complete
    print(f"\nUnwrapping {swap_amount} wNEAR to NEAR...")
    unwrap_result = unwrap_near(account, swap_amount)
    print("Successfully unwrapped wNEAR to NEAR")

if __name__ == "__main__":
    print("Running intents client tests...")
    test_near_deposit_and_withdraw()
    test_near_usdc_swap()
    test_near_sol_swap()
    test_usdc_near_swap()