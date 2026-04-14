import logging
import json
from typing import Dict, List, Any, Optional
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

class MCPClientManager:
    """
    Gestiona las conexiones a múltiples servidores MCP.
    Mantiene las sesiones abiertas y centraliza la exposición y ejecución de herramientas 
    disponibles en los servidores MCP conectados.
    """
    def __init__(self):
        # Mapea un nombre de servidor a un dict: {"session": session, "exit_stack": stack}
        self.servers: Dict[str, Dict[str, Any]] = {}
        # Mapea un nombre de herramienta al nombre del servidor que la provee
        self.tool_to_server: Dict[str, str] = {}
        # Cache de herramientas disponibles
        self.available_tools_cache: List[Dict[str, Any]] = []

    async def connect_sse_server(self, server_name: str, url: str):
        """
        Conecta a un servidor MCP utilizando SSE (Server-Sent Events).
        Ideal para despliegues en contenedores Docker o remotos.
        """
        logger.info(f"Conectando a servidor MCP '{server_name}' en {url}...")
        try:
            exit_stack = AsyncExitStack()
            
            # 1. Establecer conexión SSE
            sse_transport = await exit_stack.enter_async_context(sse_client(url))
            read_stream, write_stream = sse_transport
            
            # 2. Iniciar sesión cliente
            session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            
            # 3. Inicializar el protocolo
            await session.initialize()
            
            self.servers[server_name] = {
                "session": session,
                "exit_stack": exit_stack,
                "url": url
            }
            logger.info(f"✓ Conectado exitosamente al servidor MCP '{server_name}'")
            
            # Actualizar caché de herramientas
            await self._refresh_tools()
            
        except Exception as e:
            logger.error(f"Error conectando al servidor MCP '{server_name}': {e}")
            raise

    async def connect_stdio_server(self, server_name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None):
        """
        Conecta a un servidor MCP proceso local usando Stdio.
        """
        import asyncio
        
        logger.info(f"Iniciando servidor local MCP '{server_name}' con comando: {command} {' '.join(args)}...")
        try:
            exit_stack = AsyncExitStack()
            
            from mcp.client.stdio import stdio_client, get_default_environment
            from mcp.client.stdio import StdioServerParameters
            
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=env or get_default_environment()
            )
            
            stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))
            read_stream, write_stream = stdio_transport
            
            session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            
            self.servers[server_name] = {
                "session": session,
                "exit_stack": exit_stack,
                "command": f"{command} {' '.join(args)}"
            }
            logger.info(f"✓ Servidor local MCP '{server_name}' inicializado")
            
            await self._refresh_tools()
            
        except Exception as e:
            logger.error(f"Error levantando servidor MCP stdio '{server_name}': {e}")
            raise

    async def _refresh_tools(self):
        """Consulta todos los servidores conectados y actualiza la caché de herramientas disponibles."""
        self.available_tools_cache = []
        self.tool_to_server.clear()
        
        for server_name, server_data in self.servers.items():
            session: ClientSession = server_data["session"]
            try:
                # Obtener la lista de herramientas
                # Nota: session.list_tools() devuelve un objeto de tipo ListToolsResult
                response = await session.list_tools()
                
                for tool in response.tools:
                    # Mapear qué herramienta pertenece a qué servidor
                    self.tool_to_server[tool.name] = server_name
                    
                    # Convertir la herramienta al formato OpenAI JSON Schema
                    # para poder usarla con LLMs compatibles.
                    tool_definition = {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema
                        }
                    }
                    self.available_tools_cache.append(tool_definition)
            except Exception as e:
                logger.error(f"No se pudieron listar las herramientas del servidor {server_name}: {e}")

    def get_openai_tools(self) -> List[Dict[str, Any]]:
        """Devuelve las herramientas en formato compatible con OpenAI (tools array) para el LLM."""
        return self.available_tools_cache

    def get_langchain_tools(self) -> List[Any]:
        """Devuelve las herramientas convertidas a langchain.tools.StructuredTool"""
        from langchain.tools import StructuredTool
        from pydantic import create_model, Field
        import asyncio
        
        lc_tools = []
        for tool_def in self.available_tools_cache:
            name = tool_def["function"]["name"]
            description = tool_def["function"]["description"]
            
            # Crear un wrapper asíncrono para llamar a la herramienta en el servidor
            def create_tool_func(tool_name):
                async def _async_func(**kwargs):
                    return await self.call_tool(tool_name, kwargs)
                def _sync_func(**kwargs):
                    # Fallback síncrono enviando al event loop si LangChain lo invoca síncronamente
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import nest_asyncio
                            nest_asyncio.apply()
                        return loop.run_until_complete(self.call_tool(tool_name, kwargs))
                    except Exception:
                        return asyncio.run(self.call_tool(tool_name, kwargs))
                return _sync_func, _async_func
            
            sync_func, async_func = create_tool_func(name)
            
            # Opcional: convertir el schema dinámico a un modelo Pydantic para LangChain 
            # (En el caso general dict inputs son soportados si se configura bien)
            lc_tool = StructuredTool.from_function(
                func=sync_func,
                coroutine=async_func,
                name=name,
                description=description,
            )
            lc_tools.append(lc_tool)
            
        return lc_tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Llama a una herramienta en el servidor MCP correspondiente."""
        server_name = self.tool_to_server.get(tool_name)
        if not server_name:
            raise ValueError(f"Herramienta '{tool_name}' no encontrada en ningún servidor MCP conectado.")
        
        session: ClientSession = self.servers[server_name]["session"]
        logger.info(f"Llamando a herramienta '{tool_name}' en servidor '{server_name}' con args: {arguments}")
        
        try:
            result = await session.call_tool(tool_name, arguments)
            
            # Formatear el resultado. MCP devuelve un CallToolResult que tiene content
            if not result.content:
                return "Ejecución exitosa, sin resultados."
            
            # Combinar todos los textos o recursos devueltos
            output = []
            for item in result.content:
                if item.type == "text":
                    output.append(item.text)
                elif hasattr(item, "text"):
                    output.append(item.text)
                else:
                    output.append(str(item))
            
            return "\n".join(output)
            
        except Exception as e:
            logger.error(f"Error ejecutando la herramienta '{tool_name}': {e}")
            return f"Error ejecutando herramienta: {str(e)}"

    async def close_all(self):
        """Cierra todas las sesiones y desconecta los servidores."""
        for server_name, server_data in self.servers.items():
            try:
                await server_data["exit_stack"].aclose()
                logger.info(f"Cerrada conexión con MCP '{server_name}'")
            except Exception as e:
                logger.warning(f"Error cerrando MCP '{server_name}': {e}")
        self.servers.clear()
        self.tool_to_server.clear()
        self.available_tools_cache.clear()
