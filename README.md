## 🏃 Running the Application

1. **Initialize Oracle**:
   Run `python setup_database.py` to create tables and the advanced search PL/SQL function.
   
2. **Generate Embeddings**:
   Run `python process_vector_products.py`. This uses Gemini to turn your book descriptions into 768-dimension vectors stored in Oracle.

3. **Start the Agent**:
   Run `python main.py`. 
   
### 🛠️ Developer Commands
- To test the database independently: `python oracle_conn.py`
- To test the MCP server independently: `python server_invoice_items.py` (ensure no output on stdout)

## 🧪 Testing the Agent

Once the application is running, you can test it using natural language. The agent is designed to handle typos, partial names, and price variances.

### Example Prompts:
- **Typo Handling**: "Find the invoice for Customer 108 returning 'Hary Poter' in SP for 82.26."
- **Semantic Mapping**: "Search for an invoice for Customer 43 who bought an Orwell book in RJ."
- **Conversational**: "A client named Taylor bought the Evelyn Hugo book in SP for about 95 dollars. Find that sale."

### How to Run (Step-by-Step)
1. **Database**: Execute `python setup_database.py` (creates tables & functions).
2. **Vectors**: Execute `python process_vector_products.py` (generates embeddings).
3. **Agent**: Execute `python main.py`.

## 🛑 How to Stop the Application

To ensure Oracle database sessions are closed and background processes are terminated:

1.  **Standard Exit**: Type `exit` or `quit` in the `You:` prompt.
2.  **Emergency Exit**: Press `Ctrl+C`. The application is configured to catch this signal and perform a "Soft Landing" (closing connections before exiting).
3.  **Verification**: After closing, no `python` processes related to this project should be visible in your system task manager.