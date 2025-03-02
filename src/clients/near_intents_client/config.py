# config.py - Token configuration for Defuse Protocol

# Token data for a minimal set of tokens (NEAR and USDC)
TOKENS = [
    # USDC
    {
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "unified_asset_id": "usdc",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/3408.png",
        "chains": {
            "near": {
                "token_id": "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
                "address": "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
                "defuse_asset_id": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
            },
            "ethereum": {
                "token_id": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "defuse_asset_id": "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near",
                "omft": "eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near"
            },
            "turbochain": {
                "token_id": "0x368ebb46aca6b8d0787c96b2b20bd3cc3f2c45f7",
                "address": "0x368ebb46aca6b8d0787c96b2b20bd3cc3f2c45f7",
                "defuse_asset_id": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
            },
            "aurora": {
                "token_id": "0x368ebb46aca6b8d0787c96b2b20bd3cc3f2c45f7",
                "address": "0x368ebb46aca6b8d0787c96b2b20bd3cc3f2c45f7",
                "defuse_asset_id": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
            },
            "base": {
                "token_id": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "defuse_asset_id": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
                "omft": "base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
            },
            "arbitrum": {
                "token_id": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                "defuse_asset_id": "nep141:arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near",
                "omft": "arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near"
            },
            "solana": {
                "token_id": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "defuse_asset_id": "nep141:sol-5ce3bf3a31af18be40ba30f721101b4341690186.omft.near",
                "omft": "sol-5ce3bf3a31af18be40ba30f721101b4341690186.omft.near"
            }
        },
        "tags": ["mc:7", "type:stablecoin"]
    },
    
    # NEAR
    {
        "symbol": "NEAR",
        "name": "Near",
        "decimals": 24,
        "unified_asset_id": "near",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/6535.png",
        "chains": {
            "near": {
                "token_id": "wrap.near",
                "address": "wrap.near",
                "defuse_asset_id": "nep141:wrap.near"
            },
            "turbochain": {
                "token_id": "0xC42C30aC6Cc15faC9bD938618BcaA1a1FaE8501d",
                "address": "0xC42C30aC6Cc15faC9bD938618BcaA1a1FaE8501d",
                "defuse_asset_id": "nep141:wrap.near"
            },
            "aurora": {
                "token_id": "0xC42C30aC6Cc15faC9bD938618BcaA1a1FaE8501d",
                "address": "0xC42C30aC6Cc15faC9bD938618BcaA1a1FaE8501d",
                "defuse_asset_id": "nep141:wrap.near"
            }
        },
        "tags": ["mc:31"]
    },

    {
        "symbol": "USDT",
        "name": "Tether USD",
        "decimals": 6,
        "unified_asset_id": "usdt",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/825.png",
        "chains": {
            "near": {
                "token_id": "usdt.tether-token.near",
                "address": "usdt.tether-token.near",
                "defuse_asset_id": "nep141:usdt.tether-token.near"
            },
            "ethereum": {
                "token_id": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "defuse_asset_id": "nep141:eth-0xdac17f958d2ee523a2206206994597c13d831ec7.omft.near",
                "omft": "eth-0xdac17f958d2ee523a2206206994597c13d831ec7.omft.near"
            },
            "turbochain": {
                "token_id": "0x80Da25Da4D783E57d2FCdA0436873A193a4BEccF",
                "address": "0x80Da25Da4D783E57d2FCdA0436873A193a4BEccF",
                "defuse_asset_id": "nep141:usdt.tether-token.near"
            },
            "aurora": {
                "token_id": "0x80Da25Da4D783E57d2FCdA0436873A193a4BEccF",
                "address": "0x80Da25Da4D783E57d2FCdA0436873A193a4BEccF",
                "defuse_asset_id": "nep141:usdt.tether-token.near"
            },
            "arbitrum": {
                "token_id": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
                "address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
                "defuse_asset_id": "nep141:arb-0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9.omft.near",
                "omft": "arb-0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9.omft.near"
            },
            "solana": {
                "token_id": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                "defuse_asset_id": "nep141:sol-c800a4bd850783ccb82c2b2c7e84175443606352.omft.near",
                "omft": "sol-c800a4bd850783ccb82c2b2c7e84175443606352.omft.near"
            }
        },
        "tags": ["mc:3", "type:stablecoin"]
    },
    
    # ETH
    {
        "symbol": "ETH",
        "name": "ETH",
        "decimals": 18,
        "unified_asset_id": "eth",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/1027.png",
        "chains": {
            "ethereum": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:eth.omft.near"
            },
            "near": {
                "token_id": "aurora",
                "address": "aurora",
                "defuse_asset_id": "nep141:aurora"
            },
            "turbochain": {
                "token_id": "0x5a524251df27A25AC6b9964a93E1c23AD692688D",
                "address": "0x5a524251df27A25AC6b9964a93E1c23AD692688D",
                "defuse_asset_id": "nep141:aurora"
            },
            "aurora": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:aurora"
            },
            "base": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:base.omft.near"
            },
            "arbitrum": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:arb.omft.near"
            }
        },
        "tags": ["mc:2"]
    },
    
    # AURORA
    {
        "symbol": "AURORA",
        "name": "Aurora",
        "decimals": 18,
        "unified_asset_id": "aurora",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/14803.png",
        "chains": {
            "near": {
                "token_id": "aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near",
                "address": "aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near",
                "defuse_asset_id": "nep141:aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near"
            },
            "turbochain": {
                "token_id": "0x8BEc47865aDe3B172A928df8f990Bc7f2A3b9f79",
                "address": "0x8BEc47865aDe3B172A928df8f990Bc7f2A3b9f79",
                "defuse_asset_id": "nep141:aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near"
            },
            "aurora": {
                "token_id": "0x8BEc47865aDe3B172A928df8f990Bc7f2A3b9f79",
                "address": "0x8BEc47865aDe3B172A928df8f990Bc7f2A3b9f79",
                "defuse_asset_id": "nep141:aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near"
            },
            "ethereum": {
                "token_id": "0xAaAAAA20D9E0e2461697782ef11675f668207961",
                "address": "0xAaAAAA20D9E0e2461697782ef11675f668207961",
                "defuse_asset_id": "nep141:eth-0xaaaaaa20d9e0e2461697782ef11675f668207961.omft.near",
                "omft": "eth-0xaaaaaa20d9e0e2461697782ef11675f668207961.omft.near"
            }
        },
        "tags": ["mc:462"]
    },
    
    # TURBO
    {
        "symbol": "TURBO",
        "name": "Turbo",
        "decimals": 18,
        "unified_asset_id": "turbo",
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/24911.png",
        "chains": {
            "ethereum": {
                "token_id": "0xA35923162C49cF95e6BF26623385eb431ad920D3",
                "address": "0xA35923162C49cF95e6BF26623385eb431ad920D3",
                "defuse_asset_id": "nep141:eth-0xa35923162c49cf95e6bf26623385eb431ad920d3.omft.near",
                "omft": "eth-0xa35923162c49cf95e6bf26623385eb431ad920d3.omft.near"
            },
            "turbochain": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:a35923162c49cf95e6bf26623385eb431ad920d3.factory.bridge.near"
            },
            "near": {
                "token_id": "a35923162c49cf95e6bf26623385eb431ad920d3.factory.bridge.near",
                "address": "a35923162c49cf95e6bf26623385eb431ad920d3.factory.bridge.near",
                "defuse_asset_id": "nep141:a35923162c49cf95e6bf26623385eb431ad920d3.factory.bridge.near"
            },
            "solana": {
                "token_id": "2Dyzu65QA9zdX1UeE7Gx71k7fiwyUK6sZdrvJ7auq5wm",
                "address": "2Dyzu65QA9zdX1UeE7Gx71k7fiwyUK6sZdrvJ7auq5wm",
                "defuse_asset_id": "nep141:sol-df27d7abcc1c656d4ac3b1399bbfbba1994e6d8c.omft.near",
                "omft": "sol-df27d7abcc1c656d4ac3b1399bbfbba1994e6d8c.omft.near"
            }
        },
        "tags": ["mc:183", "type:meme"]
    },

    # BTC
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "decimals": 8,
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/1.png",
        "chains": {
            "bitcoin": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:btc.omft.near"
            }
        },
        "tags": ["mc:1"]
    },
    
    # SOL
    {
        "symbol": "SOL",
        "name": "Solana",
        "decimals": 9,
        "chains": {
            "solana": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:sol.omft.near"
            }
        },
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/5426.png",
        "tags": ["mc:6"]
    },
    
    # DOGE
    {
        "symbol": "DOGE",
        "name": "Dogecoin",
        "decimals": 8,
        "chains": {
            "dogecoin": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:doge.omft.near"
            }
        },
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/74.png",
        "tags": ["mc:8", "type:meme"]
    },
    
    # XRP
    {
        "symbol": "XRP",
        "name": "XRP",
        "decimals": 6,
        "chains": {
            "xrpledger": {
                "token_id": "native",
                "address": "native",
                "type": "native",
                "defuse_asset_id": "nep141:xrp.omft.near"
            }
        },
        "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/52.png",
        "tags": ["mc:4"]
    },

    # PEPE (Ethereum-based meme token)
{
    "symbol": "PEPE",
    "name": "Pepe",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/24478.png",
    "chains": {
        "ethereum": {
            "token_id": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
            "address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
            "defuse_asset_id": "nep141:eth-0x6982508145454ce325ddbe47a25d4ec3d2311933.omft.near",
            "omft": "eth-0x6982508145454ce325ddbe47a25d4ec3d2311933.omft.near"
        }
    },
    "tags": ["mc:30", "type:meme"]
},

# SHIB (Ethereum-based meme token)
{
    "symbol": "SHIB",
    "name": "Shiba Inu",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/5994.png",
    "chains": {
        "ethereum": {
            "token_id": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
            "address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
            "defuse_asset_id": "nep141:eth-0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce.omft.near",
            "omft": "eth-0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce.omft.near"
        }
    },
    "tags": ["mc:15", "type:meme"]
},

# LINK (Ethereum-based DeFi token)
{
    "symbol": "LINK",
    "name": "Chainlink",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/1975.png",
    "chains": {
        "ethereum": {
            "token_id": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
            "address": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
            "defuse_asset_id": "nep141:eth-0x514910771af9ca656af840dff83e8264ecf986ca.omft.near",
            "omft": "eth-0x514910771af9ca656af840dff83e8264ecf986ca.omft.near"
        }
    },
    "tags": ["mc:11"]
},

# UNI (Ethereum-based DEX token)
{
    "symbol": "UNI",
    "name": "Uniswap",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/7083.png",
    "chains": {
        "ethereum": {
            "token_id": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
            "address": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
            "defuse_asset_id": "nep141:eth-0x1f9840a85d5af5bf1d1762f925bdaddc4201f984.omft.near",
            "omft": "eth-0x1f9840a85d5af5bf1d1762f925bdaddc4201f984.omft.near"
        }
    },
    "tags": ["mc:25"]
},

# ARB (Arbitrum's native token)
{
    "symbol": "ARB",
    "name": "Arbitrum",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/11841.png",
    "chains": {
        "arbitrum": {
            "token_id": "0x912CE59144191C1204E64559FE8253a0e49E6548",
            "address": "0x912CE59144191C1204E64559FE8253a0e49E6548",
            "defuse_asset_id": "nep141:arb-0x912ce59144191c1204e64559fe8253a0e49e6548.omft.near",
            "omft": "arb-0x912ce59144191c1204e64559fe8253a0e49e6548.omft.near"
        }
    },
    "tags": ["mc:49"]
},

# AAVE (Ethereum-based lending protocol token)
{
    "symbol": "AAVE",
    "name": "Aave",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/7278.png",
    "chains": {
        "ethereum": {
            "token_id": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
            "address": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
            "defuse_asset_id": "nep141:eth-0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9.omft.near",
            "omft": "eth-0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9.omft.near"
        }
    },
    "tags": ["mc:32"]
},

# GMX (Arbitrum-based perpetuals protocol)
{
    "symbol": "GMX",
    "name": "GMX",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/11857.png",
    "chains": {
        "arbitrum": {
            "token_id": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
            "address": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
            "defuse_asset_id": "nep141:arb-0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a.omft.near",
            "omft": "arb-0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a.omft.near"
        }
    },
    "tags": ["mc:183"]
},

# SWEAT (NEAR-based token)
{
    "symbol": "SWEAT",
    "name": "Sweat Economy",
    "decimals": 18,
    "icon": "https://s2.coinmarketcap.com/static/img/coins/128x128/21351.png",
    "chains": {
        "near": {
            "token_id": "token.sweat",
            "address": "token.sweat",
            "defuse_asset_id": "nep141:token.sweat"
        }
    },
    "tags": ["mc:571"]
}
]

# Helper functions
def get_token_by_symbol(symbol, chain=None):
    """Find a token by its symbol, optionally filtered by chain."""
    for token in TOKENS:
        if token["symbol"] == symbol:
            if chain is None or chain in token.get("chains", {}):
                return token
    return None

def get_token_id(symbol, chain="near"):
    """Get the token_id for a specific token on a specific chain."""
    token = get_token_by_symbol(symbol, chain)
    if token and chain in token.get("chains", {}):
        return token["chains"][chain].get("token_id")
    return None

def get_defuse_asset_id(symbol, chain="near"):
    """Get the defuse_asset_id for a specific token on a specific chain."""
    token = get_token_by_symbol(symbol, chain)
    if token and chain in token.get("chains", {}):
        return token["chains"][chain].get("defuse_asset_id")
    return None

def to_asset_id(symbol, chain="near"):
    """Convert a token symbol to an asset ID for use in intents."""
    defuse_asset_id = get_defuse_asset_id(symbol, chain)
    if defuse_asset_id:
        return defuse_asset_id
    
    token_id = get_token_id(symbol, chain)
    if token_id:
        return f"nep141:{token_id}"
    
    return None

#ensure decimal calculations are precise
from decimal import Decimal

def to_decimals(amount, symbol, chain="near"):
    """Convert a human-readable amount to base units."""
    token = get_token_by_symbol(symbol)
    if token:
        return str(int(Decimal(str(amount)) * Decimal(10) ** token["decimals"]))
    return None

def from_decimals(amount_str, symbol):
    """Convert from base units to human-readable amount."""
    token = get_token_by_symbol(symbol)
    if token:
        return float(amount_str) / (10 ** token["decimals"])
    return None

def get_supported_tokens(chain=None):
    """Get a list of all supported tokens, optionally filtered by chain."""
    if chain:
        return [t for t in TOKENS if chain in t.get("chains", {})]
    return TOKENS

def get_supported_chains():
    """Get a list of all supported chains."""
    chains = set()
    for token in TOKENS:
        if "chains" in token:
            for chain in token["chains"].keys():
                chains.add(chain)
    return sorted(list(chains))

def get_omft_address(symbol, chain="near"):
    """Get the OMFT address for cross-chain transfers if available."""
    token = get_token_by_symbol(symbol, chain)
    if token and chain in token.get("chains", {}) and "omft" in token["chains"][chain]:
        return token["chains"][chain]["omft"]
    return None

def get_stablecoins():
    """Get all stablecoin tokens."""
    return [t for t in TOKENS if any("type:stablecoin" in t.get("tags", []))]

def get_meme_tokens():
    """Get all meme tokens."""
    return [t for t in TOKENS if any("type:meme" in t.get("tags", []))]

def get_tokens_by_market_cap_range(min_rank, max_rank):
    """Get tokens within a market cap rank range."""
    tokens = []
    for token in TOKENS:
        tags = token.get("tags", [])
        mc_tags = [tag for tag in tags if tag.startswith("mc:")]
        if mc_tags:
            mc_str = mc_tags[0].split(":")[1]
            if mc_str != "999999":  # Ignoring unranked
                mc_rank = int(mc_str)
                if min_rank <= mc_rank <= max_rank:
                    tokens.append(token)
    return tokens

# Legacy compatibility map (for backward compatibility with older code)
ASSET_MAP = {}
for token in TOKENS:
    symbol = token["symbol"]
    if "chains" in token and "near" in token["chains"]:
        ASSET_MAP[symbol] = {
            'token_id': token["chains"]["near"]["token_id"],
            'decimals': token["decimals"]
        }
        # Add OMFT if available for cross-chain support
        for chain_name, chain_data in token["chains"].items():
            if chain_name != "near" and "omft" in chain_data:
                if "omft" not in ASSET_MAP[symbol]:
                    ASSET_MAP[symbol]['omft'] = chain_data["omft"]
                break