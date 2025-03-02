from typing import TypedDict, List, Dict, Union
import borsh_construct
import os
import json
import base64
import base58
import random
import requests
import near_api
from . import config
from src.clients.near_intents_client.config import (
    get_token_id,
    to_asset_id,
    to_decimals,
    from_decimals,
    get_token_by_symbol,
    get_omft_address,
    get_defuse_asset_id
)
from dotenv import load_dotenv
import time
import logging

load_dotenv()

MAX_GAS = 300 * 10 ** 12
SOLVER_BUS_URL = os.getenv('SOLVER_BUS_URL', "https://solver-relay-v2.chaindefuser.com/rpc")

# Configure logger
logger = logging.getLogger(__name__)

class Intent(TypedDict):
    intent: str
    diff: Dict[str, str]


class Quote(TypedDict):
    nonce: str
    signer_id: str
    verifying_contract: str
    deadline: str
    intents: List[Intent]


def quote_to_borsh(quote):
    QuoteSchema = borsh_construct.CStruct(
        'nonce' / borsh_construct.String,
        'signer_id' / borsh_construct.String,
        'verifying_contract' / borsh_construct.String,
        'deadline' / borsh_construct.String,
        'intents' / borsh_construct.Vec(borsh_construct.CStruct(
            'intent' / borsh_construct.String,
            'diff' / borsh_construct.HashMap(borsh_construct.String, borsh_construct.String)
        ))
    )
    return QuoteSchema.build(quote)


class AcceptQuote(TypedDict):
    nonce: str
    recipient: str
    message: str


class Commitment(TypedDict):
    standard: str
    payload: Union[AcceptQuote, str]
    signature: str
    public_key: str


class SignedIntent(TypedDict):
    signed: List[Commitment]
    

class PublishIntent(TypedDict):
    signed_data: Commitment
    quote_hashes: List[str] = []


def account(account_path):
    RPC_NODE_URL = 'https://rpc.mainnet.near.org'
    content = json.load(open(os.path.expanduser(account_path), 'r'))
    near_provider = near_api.providers.JsonProvider(RPC_NODE_URL)
    key_pair = near_api.signer.KeyPair(content["private_key"])
    signer = near_api.signer.Signer(content["account_id"], key_pair)
    return near_api.account.Account(near_provider, signer, content["account_id"])


def get_asset_id(token):
    return config.to_asset_id(token)


def register_token_storage(account, token, other_account=None):
    """Register token storage for an account"""
    account_id = other_account if other_account else account.account_id
    token_id = config.get_token_id(token)
    if not token_id:
        raise ValueError(f"Token {token} not supported")
        
    balance = account.view_function(token_id, 'storage_balance_of', {'account_id': account_id})['result']
    if not balance:
        logger.info('Register %s for %s storage' % (account_id, token))
        account.function_call(token_id, 'storage_deposit',
            {"account_id": account_id}, MAX_GAS, 1250000000000000000000)


def sign_quote(account, quote):
    quote_data = quote.encode('utf-8')
    signature = 'ed25519:' + base58.b58encode(account.signer.sign(quote_data)).decode('utf-8')
    public_key = 'ed25519:' + base58.b58encode(account.signer.public_key).decode('utf-8')
    return Commitment(standard="raw_ed25519", payload=quote, signature=signature, public_key=public_key)


def create_token_diff_quote(account, token_in, amount_in, token_out, amount_out, quote_asset_in=None, quote_asset_out=None):
    """Create a token diff quote for swapping"""
    # Use config's asset ID helpers
    token_in_fmt = quote_asset_in if quote_asset_in else config.get_defuse_asset_id(token_in)
    token_out_fmt = quote_asset_out if quote_asset_out else config.get_defuse_asset_id(token_out)
    
    if not token_in_fmt or not token_out_fmt:
        raise ValueError(f"Token {token_in} or {token_out} not supported")
        
    nonce = base64.b64encode(random.getrandbits(256).to_bytes(32, byteorder='big')).decode('utf-8')
    quote = json.dumps(Quote(
        signer_id=account.account_id,
        nonce=nonce,
        verifying_contract="intents.near",
        deadline=get_future_deadline(),
        intents=[
            Intent(intent='token_diff', diff={
                token_in_fmt: f"-{str(amount_in)}",
                token_out_fmt: str(amount_out)
            })
        ]
    ))
    return sign_quote(account, quote)


def submit_signed_intent(account, signed_intent):
    account.function_call("intents.near", "execute_intents", signed_intent, MAX_GAS, 0)


def wrap_near(account, amount):
    """
    Wrap NEAR into wNEAR
    Args:
        account: NEAR account
        amount: Amount of NEAR to wrap
    """
    try:
        # Use config's decimal conversion instead of hardcoded
        amount_base = config.to_decimals(amount, "NEAR")
        if not amount_base:
            raise ValueError("Invalid NEAR amount")
            
        return account.function_call(
            'wrap.near',
            'near_deposit',
            {},
            MAX_GAS,
            int(amount_base)
        )
    except Exception as e:
        logger.error(f"Error wrapping NEAR: {str(e)}")
        raise e


def unwrap_near(account, amount):
    """
    Unwrap wNEAR back to NEAR
    Args:
        account: NEAR account
        amount: Amount of wNEAR to unwrap
    """
    try:
        # Use config's decimal conversion
        amount_base = config.to_decimals(amount, "NEAR")
        if not amount_base:
            raise ValueError("Invalid NEAR amount")
            
        return account.function_call(
            'wrap.near',
            'near_withdraw',
            {"amount": amount_base},
            MAX_GAS,
            1  # Attach exactly 1 yoctoNEAR as required by the contract
        )
    except Exception as e:
        logger.error(f"Error unwrapping NEAR: {str(e)}")
        raise e


def intent_deposit(account, token, amount):
    """Deposit tokens into the intents contract"""
    token_id = config.get_token_id(token)
    if not token_id:
        raise ValueError(f"Token {token} not supported")
        
    register_token_storage(account, token, other_account="intents.near")
    
    # Use config's decimal conversion
    amount_base = config.to_decimals(amount, token)
    if not amount_base:
        raise ValueError(f"Invalid amount for {token}")
        
    account.function_call(token_id, 'ft_transfer_call', {
        "receiver_id": "intents.near",
        "amount": amount_base,
        "msg": ""
    }, MAX_GAS, 1)


def register_intent_public_key(account):
    account.function_call("intents.near", "add_public_key", {
        "public_key": "ed25519:" + base58.b58encode(account.signer.public_key).decode('utf-8')
    }, MAX_GAS, 1)


class IntentRequest(object):
    """IntentRequest is a request to perform an action on behalf of the user."""
    
    def __init__(self, request=None, thread=None, min_deadline_ms=120000):
        self.request = request
        self.thread = thread
        self.min_deadline_ms = min_deadline_ms

    def asset_in(self, asset_name, amount, chain="near"):
        self.asset_in = {
            "asset": config.to_asset_id(asset_name, chain),
            "amount": config.to_decimals(amount, asset_name)
        }
        return self

    def asset_out(self, asset_name, amount=None, chain="ethereum"):
        self.asset_out = {
            "asset": config.to_asset_id(asset_name, chain),
            "amount": config.to_decimals(amount, asset_name) if amount else None,
            "chain": chain
        }
        return self

    def serialize(self):
        message = {
            "defuse_asset_identifier_in": self.asset_in["asset"],
            "defuse_asset_identifier_out": self.asset_out["asset"],
            "exact_amount_in": str(self.asset_in["amount"]),
            "exact_amount_out": str(self.asset_out["amount"]),
            "min_deadline_ms": self.min_deadline_ms,
        }
        if self.asset_in["amount"] is None:
            del message["exact_amount_in"]
        if self.asset_out["amount"] is None:
            del message["exact_amount_out"]
        return message


def fetch_options(request):
    """Fetches the trading options from the solver bus."""
    rpc_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "quote",
        "params": [{
            "defuse_asset_identifier_in": request.asset_in["asset"],
            "defuse_asset_identifier_out": request.asset_out["asset"],
            "exact_amount_in": str(request.asset_in["amount"])
        }]
    }
    
    try:
        response = requests.post(SOLVER_BUS_URL, json=rpc_request)
        if response.status_code != 200:
            logger.error(f"Error from solver bus: {response.text}")
            return []
            
        result = response.json()
        if "error" in result:
            logger.error(f"RPC error: {result['error']}")
            return []
            
        quotes = result.get("result", [])
        if not quotes:
            logger.info("No quotes available for this swap")
            
        return quotes
            
    except Exception as e:
        logger.error(f"Error fetching quotes: {str(e)}")
        return []


def publish_intent(signed_intent):
    """Publishes the signed intent to the solver bus."""
    rpc_request = {
        "id": "dontcare",
        "jsonrpc": "2.0",
        "method": "publish_intent",
        "params": [signed_intent]
    }
    response = requests.post(SOLVER_BUS_URL, json=rpc_request)
    return response.json()


def select_best_option(options):
    """Selects the best option from the list of options."""
    best_option = None
    for option in options:
        if not best_option or option["amount_out"] > best_option["amount_out"]:
            best_option = option
    return best_option


def intent_swap(account, token_in: str, amount_in: float, token_out: str, chain_out: str = "ethereum") -> dict:
    """Execute a token swap using intents."""
    # Validate tokens exist on respective chains
    if not config.get_token_by_symbol(token_in):
        raise ValueError(f"Token {token_in} not supported")
    if not config.get_token_by_symbol(token_out, chain_out):
        raise ValueError(f"Token {token_out} not supported on {chain_out}")
    
    # Convert amount using config helper
    amount_in_base = config.to_decimals(amount_in, token_in)
    
    # Get quote from solver
    request = IntentRequest().asset_in(token_in, amount_in).asset_out(token_out, chain=chain_out)
    options = fetch_options(request)
    best_option = select_best_option(options)
    
    if not best_option:
        raise Exception("No valid quotes received")
    
    # Create quote using proper asset identifiers
    quote = create_token_diff_quote(
        account,
        token_in,
        amount_in_base,
        token_out,
        best_option['amount_out'],
        quote_asset_in=best_option['defuse_asset_identifier_in'],
        quote_asset_out=best_option['defuse_asset_identifier_out']
    )
    
    # Submit intent
    signed_intent = PublishIntent(
        signed_data=quote,
        quote_hashes=[best_option['quote_hash']]
    )
    
    return {
        **publish_intent(signed_intent),
        'amount_out': best_option['amount_out']
    }


def get_future_deadline(days=365):
    """Generate a deadline timestamp that's X days in the future"""
    from datetime import datetime, timedelta, UTC
    future_date = datetime.now(UTC) + timedelta(days=days)
    return future_date.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def get_intent_balance(account, token, chain="near"):
    """
    Get the balance of a specific token in the intents contract for an account
    Args:
        account: NEAR account
        token: Token symbol (e.g., 'USDC', 'NEAR', 'ETH')
        chain: Chain name (e.g., 'near', 'ethereum') - defaults to 'near'
    Returns:
        float: The balance in human-readable format
    """
    # Get the defuse asset ID for the specific chain
    nep141_token_id = get_defuse_asset_id(token, chain)
    
    if not nep141_token_id:
        raise ValueError(f"Token {token} not supported on chain {chain}")
    
    try:
        balance_response = account.view_function(
            'intents.near',
            'mt_balance_of',
            {
                'token_id': nep141_token_id,
                'account_id': account.account_id
            }
        )
        
        if balance_response and 'result' in balance_response:
            token_info = get_token_by_symbol(token)
            decimals = token_info['decimals'] if token_info else 6
            return float(balance_response['result']) / (10 ** decimals)
    except Exception as e:
        logger.error(f"Error getting balance: {str(e)}")
    return 0.0


def smart_withdraw(account, token: str, amount: float, destination_address: str = None, destination_chain: str = None, source_chain: str = None) -> dict:
    """
    Smart router that picks the appropriate withdrawal method
    Args:
        account: NEAR account
        token: Token symbol (e.g., 'USDC', 'NEAR')
        amount: Amount to withdraw
        destination_address: Address to withdraw to (defaults to account.account_id)
        destination_chain: Chain to withdraw to (defaults to "near")
        source_chain: Chain where token currently is (e.g., "ethereum" for ETH-USDC)
    """
    if not destination_chain:
        destination_chain = "near"
        
    if destination_chain == "near":
        return withdraw_same_chain(account, token, amount, destination_address, source_chain)
    else:
        return withdraw_cross_chain(account, token, amount, destination_chain, destination_address)


def withdraw_same_chain(account, token: str, amount: float, destination_address: str = None, source_chain: str = None) -> dict:
    """
    Withdraw tokens to same chain (e.g., NEAR to NEAR wallet)
    If token is on another chain, handles conversion first
    """
    token_id = config.get_token_id(token, "near")
    destination_address = destination_address or account.account_id
    
    # For NEAR token, we're always on NEAR chain
    if token == "NEAR":
        source_chain = "near"
    elif source_chain is None:
        # Check balances to determine source chain
        for chain in ["ethereum", "near", "arbitrum", "solana"]:
            balance = get_intent_balance(account, token, chain=chain)
            if balance >= amount:
                source_chain = chain
                break
                
    if not source_chain:
        raise ValueError(f"Could not find source chain for {token} with sufficient balance")
    
    # Check if token needs conversion to NEAR chain
    current_chain_asset = config.get_defuse_asset_id(token, source_chain)
    near_chain_asset = config.get_defuse_asset_id(token, "near")
    
    if current_chain_asset != near_chain_asset and source_chain != "near":
        logger.info(f"\nConverting {token} from {source_chain} to NEAR chain...")
        # Need conversion quote first
        request = IntentRequest().asset_in(token, amount, chain=source_chain).asset_out(token, chain="near")
        options = fetch_options(request)
        best_option = select_best_option(options)
        
        if not best_option:
            raise Exception(f"No conversion quote available for {token} to NEAR chain")
        
        # Create and publish conversion quote first
        conversion_quote = create_token_diff_quote(
            account,
            token,
            config.to_decimals(amount, token),
            token,
            best_option['amount_out'],
            quote_asset_in=best_option['defuse_asset_identifier_in'],
            quote_asset_out=best_option['defuse_asset_identifier_out']
        )
        
        # Submit conversion intent
        conversion_intent = PublishIntent(
            signed_data=conversion_quote,
            quote_hashes=[best_option['quote_hash']]
        )
        
        conversion_result = publish_intent(conversion_intent)
        logger.info("\nConversion Result:")
        logger.info(json.dumps(conversion_result, indent=2))
        
        # Use converted amount for withdrawal
        amount_base = best_option['amount_out']
        time.sleep(3)  # Give time for conversion to complete
    else:
        # No conversion needed, use direct amount
        amount_base = config.to_decimals(amount, token)
    
    # Now do the withdrawal with converted amount
    quote = Quote(
        signer_id=account.account_id,
        nonce=base64.b64encode(random.getrandbits(256).to_bytes(32, byteorder='big')).decode('utf-8'),
        verifying_contract="intents.near",
        deadline=get_future_deadline(),
        intents=[{
            "intent": "ft_withdraw",
            "token": token_id,
            "receiver_id": destination_address,
            "amount": amount_base
        }]
    )
    
    signed_quote = sign_quote(account, json.dumps(quote))
    signed_intent = PublishIntent(signed_data=signed_quote)
    return publish_intent(signed_intent)


def withdraw_cross_chain(account, token: str, amount: float, destination_chain: str, destination_address: str = None) -> dict:
    """Withdraw tokens to different chain"""
    # Get token config and validate
    token_config = config.get_token_by_symbol(token)
    if not token_config:
        raise ValueError(f"Token {token} not supported")
    
    # Get destination address
    if not destination_address:
        if destination_chain == "solana":
            destination_address = os.getenv('SOLANA_ACCOUNT_ID')
        elif destination_chain in ["ethereum", "arbitrum", "base"]:
            destination_address = os.getenv('ETHEREUM_ACCOUNT_ID')
            
    if not destination_address:
        raise ValueError(f"No destination address provided for {destination_chain} chain")
    
    # Get the exact token ID that matches our balance
    defuse_asset_id = config.get_defuse_asset_id(token, destination_chain)
    if not defuse_asset_id:
        raise ValueError(f"No defuse asset ID for {token} on {destination_chain}")
        
    # Remove 'nep141:' prefix to get the token ID
    token_id = defuse_asset_id.replace('nep141:', '')
    
    logger.info(f"\nWithdrawal Details:")
    logger.info(f"Token: {token}")
    logger.info(f"Chain: {destination_chain}")
    logger.info(f"Token ID: {token_id}")
    logger.info(f"Destination: {destination_address}")
    
    amount_base = config.to_decimals(amount, token)
    
    quote = Quote(
        signer_id=account.account_id,
        nonce=base64.b64encode(random.getrandbits(256).to_bytes(32, byteorder='big')).decode('utf-8'),
        verifying_contract="intents.near",
        deadline=get_future_deadline(),
        intents=[{
            "intent": "ft_withdraw",
            "token": token_id,
            "receiver_id": token_id,
            "amount": amount_base,
            "memo": f"WITHDRAW_TO:{destination_address}"
        }]
    )
    
    signed_quote = sign_quote(account, json.dumps(quote))
    signed_intent = PublishIntent(signed_data=signed_quote)
    return publish_intent(signed_intent)


def deposit_token(account, token: str, amount: float, source_chain: str = None) -> dict:
    """Deposit any supported token into intents contract"""
    if token == "NEAR":
        # Existing NEAR flow
        wrap_result = wrap_near(account, amount)
        time.sleep(3)
        return intent_deposit(account, token, amount)
    else:
        # New flow for other tokens
        token_id = config.get_token_id(token, source_chain)
        if not token_id:
            raise ValueError(f"Token {token} not supported on {source_chain}")
            
        # Register storage if needed
        register_token_storage(account, token)
        
        # Execute deposit
        return intent_deposit(account, token, amount)


if __name__ == "__main__":
    # Trade between two accounts directly.
    # account1 = utils.account(
    #     "<>")
    # account2 = utils.account(
    #     "<>")
    # register_intent_public_key(account1)
    # register_intent_public_key(account2)
    # intent_deposit(account1, 'NEAR', 1)
    # intent_deposit(account2, 'USDC', 10)
    # quote1 = create_token_diff_quote(account1, 'NEAR', '1', 'USDC', '8')
    # quote2 = create_token_diff_quote(account2, 'USDC', '8', 'NEAR', '1')
    # signed_intent = SignedIntent(signed=[quote1, quote2])
    # print(json.dumps(signed_intent, indent=2))
    # submit_signed_intent(account1, signed_intent)

    # Trade via solver bus.
    # account1 = account("")
    # print(intent_swap(account1, 'NEAR', 1, 'USDC'))

    # Withdraw to external address.
    account1 = account("<>")
    # print(intent_withdraw(account1, "<near account>", "USDC", 1))
    print(intent_withdraw(account1, "<eth address>", "USDC", 1, network='ethereum'))