import asyncio
import os
import sys
from pathlib import Path
from rich.console import Console
from dotenv import load_dotenv

# Add src directory to Python path
src_path = str(Path(__file__).parent.parent)
if src_path not in sys.path:
    sys.path.append(src_path)

from src.tools.time_tools import TimeTool

console = Console()

async def test_get_time():
    """Test getting current time for a location"""
    try:
        tool = TimeTool()
        
        while True:
            console.print("\n[bold cyan]Time Tool Test - Get Current Time[/]")
            console.print("[yellow]Enter a location/timezone (e.g., 'Tokyo' or 'America/New_York')[/]")
            console.print("[yellow]Or type 'q' to quit[/]")
            
            location = input().strip()
            if location.lower() == 'q':
                break
                
            console.print(f"\n[cyan]Getting time for {location}...[/]")
            result = await tool.get_current_time_in_zone(location)
            
            if result["status"] == "success":
                console.print("\n[bold green]Results:[/]")
                console.print(f"Location: {result['location']}")
                console.print(f"Timezone: {result['timezone']}")
                console.print(f"Current Time: {result['current_time']}")
                console.print(f"Day: {result['day_of_week']}")
                console.print(f"DST Active: {result['dst_active']}")
            else:
                console.print(f"\n[bold red]Error: {result['message']}[/]")

    except Exception as e:
        console.print(f"[bold red]Error: {str(e)}[/]")

async def test_convert_time():
    """Test converting time between zones"""
    try:
        tool = TimeTool()
        
        while True:
            console.print("\n[bold cyan]Time Tool Test - Convert Time[/]")
            console.print("[yellow]Enter source location (or 'q' to quit):[/]")
            from_zone = input().strip()
            if from_zone.lower() == 'q':
                break
                
            console.print("[yellow]Enter time (e.g., '2pm' or '14:00'):[/]")
            time_str = input().strip()
            
            console.print("[yellow]Enter target location:[/]")
            to_zone = input().strip()
            
            console.print(f"\n[cyan]Converting {time_str} from {from_zone} to {to_zone}...[/]")
            result = await tool.convert_time_between_zones(
                from_zone=from_zone,
                date_time=time_str,
                to_zone=to_zone
            )
            
            if result["status"] == "success":
                console.print("\n[bold green]Results:[/]")
                console.print(f"From: {result['from_time']} ({result['from_timezone']})")
                console.print(f"To: {result['converted_time']} ({result['to_timezone']})")
            else:
                console.print(f"\n[bold red]Error: {result['message']}[/]")

    except Exception as e:
        console.print(f"[bold red]Error: {str(e)}[/]")

async def main():
    try:
        load_dotenv()
        
        while True:
            console.print("\n[bold cyan]Time Tool Tests[/]")
            console.print("1. Test Get Current Time")
            console.print("2. Test Time Conversion")
            console.print("q. Quit")
            
            choice = input("\nSelect an option: ").strip().lower()
            
            if choice == 'q':
                break
            elif choice == '1':
                await test_get_time()
            elif choice == '2':
                await test_convert_time()
            else:
                console.print("[yellow]Invalid option, try again[/]")
                
    except Exception as e:
        console.print(f"[bold red]Fatal error: {str(e)}[/]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Test terminated by user[/]")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {str(e)}[/]") 