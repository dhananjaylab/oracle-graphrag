import asyncio
import json
import os
import sys
from pathlib import Path
from google import genai
from decouple import config
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# Configuration
GEMINI_API_KEY = config("GEMINI_API_KEY")
print(f"🔑 GEMINI_API_KEY: {GEMINI_API_KEY}", file=sys.stderr)
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY environment variable is required")

# Initialize the Modern Client
client = genai.Client(api_key=GEMINI_API_KEY)

# System prompt for the agent
SYSTEM_PROMPT = """You are an Expert Invoice Reconciliation Agent. 
Your task is to match customer returns to original outbound invoices.

### Protocol:
1. Identify the product using semantic search.
2. Cross-reference the EAN with the Invoice database.
3. Provide a 'Confidence Score' (0-100%):
   - 100%: EAN, Price, and Customer match exactly.
   - 80%: EAN and Customer match, but Price is slightly different (within margin).
   - 50%: Product matches semantically but Customer name has slight variations.

### Output Format:
| Invoice # | Product | Customer | Match Score | Reasoning |
|-----------|---------|----------|-------------|-----------|
| ...       | ...     | ...      | ...         | ...       |
"""

# Tool definitions using the modern SDK style
# We pass these as a list of tools to the model
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_vectorized_product",
                description="Searches for a product by description using semantic embeddings. Returns similar products with codes and descriptions.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "description": types.Schema(type="STRING", description="The product description to search for")
                    },
                    required=["description"]
                )
            ),
            types.FunctionDeclaration(
                name="resolve_ean",
                description="Resolves the product's EAN code based on its description using advanced search techniques.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "description": types.Schema(type="STRING", description="The product description to resolve an EAN for")
                    },
                    required=["description"]
                )
            ),
            types.FunctionDeclaration(
                name="search_invoices_by_criteria",
                description="Searches for A/R invoices based on customer, state, EAN, and/or price.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "customer": types.Schema(type="STRING", description="Customer name"),
                        "state": types.Schema(type="STRING", description="State code (e.g., SP, RJ)"),
                        "ean": types.Schema(type="STRING", description="Product EAN code"),
                        "price": types.Schema(type="NUMBER", description="Product price"),
                        "margin": types.Schema(type="NUMBER", description="Price margin (default: 0.05)")
                    }
                )
            )
        ]
    )
]

class MemoryState:
    """Conversation history for the modern SDK"""
    def __init__(self):
        self.history = []

    def add_user_message(self, content):
        self.history.append(types.Content(role="user", parts=[types.Part(text=content)]))

    def add_assistant_message(self, parts):
        self.history.append(types.Content(role="model", parts=parts))

    def add_tool_result(self, call_id, tool_name, result):
        """Add tool execution result to history"""
        self.history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=tool_name,
                            response={"result": result}
                        )
                    )
                ]
            )
        )

async def call_mcp_tool(mcp_session: ClientSession, tool_name: str, tool_input: dict) -> dict:
    """Call a tool through the MCP server"""
    try:
        result = await mcp_session.call_tool(tool_name, tool_input)
        # Assuming MCP server returns text/json in the first content block
        return result.content[0].text if result.content else {}
    except Exception as e:
        return {"error": str(e)}

async def run_agent_loop(mcp_session: ClientSession, query: str, memory_state: MemoryState):
    """Modern ReAct Agent Loop"""
    memory_state.add_user_message(query)
    
    print("\n🤖 Agent Processing...", file=sys.stderr)
    
    while True:
        try:
            # Generate content using history and tools
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=TOOLS,
                    temperature=0.1
                ),
                contents=memory_state.history
            )
        except Exception as e:
            print(f"❌ Error calling Gemini: {e}", file=sys.stderr)
            break

        if not response.candidates:
            break

        candidate = response.candidates[0]
        # Store model's turn in history
        memory_state.add_assistant_message(candidate.content.parts)

        # Check for function calls
        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        if not function_calls:
            # Final text response
            for part in candidate.content.parts:
                if part.text:
                    print(f"\n✅ Assistant: {part.text}", file=sys.stderr)
            break

        # Process function calls
        for fc in function_calls:
            print(f"🔧 Tool: {fc.name} | Args: {fc.args}", file=sys.stderr)
            
            # Execute via MCP
            result = await call_mcp_tool(mcp_session, fc.name, fc.args)
            
            # Feed back to history
            memory_state.add_tool_result(None, fc.name, result)

async def main():
    print("🚀 Starting MCP Client...", file=sys.stderr)
    
    server_script = Path(__file__).parent / "server_invoice_items.py"

    # Use -u for unbuffered output to ensure JSON-RPC is clean
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-u", str(server_script)],
        env=os.environ.copy()
    )
    
    try:
        # 1. Establish the Transport (Pipes)
        async with stdio_client(server_params) as (read_stream, write_stream):
            
            # 2. Create the Session using the pipes
            async with ClientSession(read_stream, write_stream) as session:
                
                # 3. Initialize the Session (The Handshake)
                await session.initialize()
                print("✅ MCP Session Initialized", file=sys.stderr)
                
                memory_state = MemoryState()
                
                print("\n" + "="*60)
                print("🎯 Invoice Resolution Agent (v2.0)")
                print("="*60)
                print("Type 'exit' to quit. Use Ctrl+C to force stop.\n")
                
                # Inside main.py
                while True:
                    try:
                        # Prompt user
                        query = input("\nYou (type 'exit' to quit): ").strip()
                        
                        if query.lower() in ["quit", "exit", "bye"]:
                            print("\n👋 Shutting down agent and closing MCP session...", file=sys.stderr)
                            break # Breaks out of 'while True', triggering the 'async with' exit
                            
                        if not query: continue
                        
                        await run_agent_loop(session, query, memory_state)
                        
                    except KeyboardInterrupt:
                        print("\n\n⚠️  Interrupt detected. Cleaning up resources...", file=sys.stderr)
                        break # Exit smoothly on Ctrl+C
                    except Exception as e:
                        print(f"\n❌ Runtime Error: {e}", file=sys.stderr)

                print("✅ Shutdown complete. Goodbye.", file=sys.stderr)

    except Exception as e:
        print(f"\n❌ Connection failed: {e}", file=sys.stderr)
        # If the server is spitting out errors, this will catch them
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())