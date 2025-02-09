import asyncio
import websockets
import json
import logging
from aiohttp import web
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class ChatWebSocketServer:
    def __init__(self, host='localhost', port=8765):
        self.host = host
        self.port = port
        self.clients = set()
        
    async def register(self, websocket):
        """Register a new client"""
        self.clients.add(websocket)
        logger.info(f"New client connected. Total clients: {len(self.clients)}")
        
        # Send welcome message
        welcome_msg = {
            'author': 'System',
            'content': 'Connected to chat server',
            'timestamp': datetime.now().isoformat()
        }
        await websocket.send_str(json.dumps(welcome_msg))

    async def unregister(self, websocket):
        """Unregister a client"""
        self.clients.remove(websocket)
        logger.info(f"Client disconnected. Total clients: {len(self.clients)}")

    async def broadcast_message(self, message):
        """Broadcast message to all connected clients"""
        if not self.clients:
            return
            
        formatted_message = {
            'author': message.get('author', 'System'),
            'content': message.get('content', ''),
            'timestamp': message.get('timestamp', datetime.now().isoformat())
        }
        
        # Create a copy of clients to avoid runtime modification issues
        clients = self.clients.copy()
        for client in clients:
            try:
                await client.send_str(json.dumps(formatted_message))
            except websockets.exceptions.ConnectionClosed:
                await self.unregister(client)
            except Exception as e:
                logger.error(f"Error sending message to client: {e}")

    async def websocket_handler(self, request):
        """Handle WebSocket connections"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        await self.register(ws)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Handle any incoming messages if needed
                    pass
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f'WebSocket connection closed with error: {ws.exception()}')
        finally:
            await self.unregister(ws)
        return ws

    async def serve_static(self, request):
        """Serve static files"""
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
        return web.FileResponse(os.path.join(static_dir, 'index.html'))

    async def start(self):
        """Start HTTP server with WebSocket support"""
        try:
            # Create HTTP app
            app = web.Application()
            
            # Add routes
            app.router.add_get('/', self.serve_static)
            app.router.add_get('/ws', self.websocket_handler)  # Add WebSocket endpoint
            
            # Start server
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            
            logger.info(f"Server started at http://{self.host}:{self.port}")
            logger.info(f"WebSocket endpoint at ws://{self.host}:{self.port}/ws")
            
            return runner
            
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
            raise 