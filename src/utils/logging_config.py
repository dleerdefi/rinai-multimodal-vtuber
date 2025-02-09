import logging
from rich.logging import RichHandler
from rich.console import Console
import sys

console = Console()

def setup_logging():
    """Configure logging with rich handler"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)]
    )

    # Get the root logger
    root_logger = logging.getLogger()
    
    # Remove any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Add our rich handler
    rich_handler = RichHandler(console=console, rich_tracebacks=True)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(rich_handler)
    
    # Set levels for different loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    return console