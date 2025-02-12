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

from src.tools.weather_tools import WeatherTool
from src.tools.base import WeatherToolParameters

console = Console()

async def test_weather():
    """Test getting weather data for a location"""
    try:
        tool = WeatherTool()
        
        while True:
            console.print("\n[bold cyan]Weather Tool Test[/]")
            console.print("[yellow]Enter a weather query (e.g., 'weather in Tokyo' or 'forecast for London')[/]")
            console.print("[yellow]Or type 'q' to quit[/]")
            
            query = input().strip()
            if query.lower() == 'q':
                break
            
            console.print("\n[yellow]Select units (metric/imperial):[/]")
            units = input().strip().lower()
            if units not in ['metric', 'imperial']:
                units = 'metric'
            
            # Create tool parameters
            params = WeatherToolParameters(location=query, units=units)
            
            console.print(f"\n[cyan]Getting weather data for: {query}[/]")
            result = await tool.run(params)
            
            if result["status"] == "success":
                console.print("\n[bold green]Current Conditions:[/]")
                current = result["current"]
                console.print(f"Location: {result['location']}")
                console.print(f"Temperature: {current['temperature']}")
                console.print(f"Humidity: {current['humidity']}")
                console.print(f"Precipitation: {current['precipitation']}")
                console.print(f"Wind Speed: {current['wind_speed']}")
                
                if "forecast" in result:
                    console.print("\n[bold green]Forecast:[/]")
                    forecast = result["forecast"]
                    
                    if "intervals" in forecast:  # Hourly forecast
                        for interval in forecast["intervals"][:6]:  # Show next 6 hours
                            console.print(f"\nTime: {interval['time']}")
                            console.print(f"Temperature: {interval['temperature']}")
                            console.print(f"Precipitation: {interval['precipitation_prob']}")
                            console.print(f"Wind Speed: {interval['wind_speed']}")
                    
                    elif "days" in forecast:  # Daily forecast
                        for day in forecast["days"]:
                            console.print(f"\nDate: {day['date']}")
                            console.print(f"Temperature: {day['temperature']}")
                            console.print(f"Precipitation: {day['precipitation_prob']}")
                            console.print(f"Wind Speed: {day['wind_speed']}")
            else:
                console.print(f"\n[bold red]Error: {result['message']}[/]")

    except Exception as e:
        console.print(f"[bold red]Error: {str(e)}[/]")

async def main():
    try:
        load_dotenv()
        await test_weather()
                
    except Exception as e:
        console.print(f"[bold red]Fatal error: {str(e)}[/]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Test terminated by user[/]")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {str(e)}[/]") 