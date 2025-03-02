import os
import logging
from near_api.account import Account
from near_api.signer import KeyPair, Signer
from near_api.providers import JsonProvider

logger = logging.getLogger(__name__)

def get_near_account():
    """Create and return a NEAR account instance from environment variables"""
    try:
        account_id = os.getenv('NEAR_ACCOUNT_ID')
        private_key = os.getenv('NEAR_PRIVATE_KEY')
        rpc_url = os.getenv('NEAR_RPC_URL')
        
        if not account_id or not private_key or not rpc_url:
            logger.error("Missing NEAR account environment variables")
            return None
            
        provider = JsonProvider(rpc_url)
        key_pair = KeyPair(private_key)
        signer = Signer(account_id, key_pair)
        account = Account(provider, signer)
        
        logger.info(f"Successfully initialized NEAR account: {account_id}")
        return account
        
    except Exception as e:
        logger.error(f"Failed to initialize NEAR account: {e}")
        return None 