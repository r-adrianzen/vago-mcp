# VAGO Cloud MCP Server

![CI](https://github.com/r-adrianzen/vago-mcp/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

Official [Model Context Protocol](https://modelcontextprotocol.io) server for **VAGO Cloud**.

Connect AI agents (Claude Code, Cursor, Cowork, etc.) to the VAGO Cloud API and let them inspect tenants, jobs, stock, channels, and analytics — or perform allowed actions like retrying a failed sync.

> **VAGO Cloud** syncs stock, pricing and products between a master catalog (Excel / OneDrive / Shopify) and marketplaces like Shopify, MercadoLibre, Falabella, Ripley and more.

## Requirements

- Python ≥ 3.10
- A VAGO Cloud account with a valid **agent token** (`vago_agt_...`)

## Installation

```bash
pip install -r requirements.txt
```

Or install directly from this repo:

```bash
pip install git+https://github.com/r-adrianzen/vago-mcp.git
```

## Getting an agent token

1. Log in to VAGO Cloud as a superadmin.
2. Go to **Gestión de cuenta → Agentes IA → Crear token**.
3. Copy the token immediately (it is shown only once).

Tokens can be revoked from the same screen. There is also a global kill switch at **Gestión de cuenta → Agentes IA → "Apagar acceso"**.

## Usage

### Claude Code

Add to your project `claude.md` or global Claude config:

```json
{
  "mcpServers": {
    "vago-cloud": {
      "command": "python",
      "args": ["/path/to/vago-mcp/server.py"],
      "env": {
        "VAGO_API_URL": "https://www.vagocloud.com",
        "VAGO_AGENT_TOKEN": "vago_agt_..."
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "vago-cloud": {
      "command": "python",
      "args": ["C:/path/to/vago-mcp/server.py"],
      "env": {
        "VAGO_API_URL": "https://www.vagocloud.com",
        "VAGO_AGENT_TOKEN": "vago_agt_..."
      }
    }
  }
}
```

### HTTP transport (remote / co-located)

Set `VAGO_MCP_TRANSPORT=http` and `VAGO_MCP_SHARED_SECRET` to expose the server over HTTP. See `server.py` for details.

## Available tools

- **Identity & platform:** `whoami`, `platform_overview`
- **Tenants:** `list_tenants`, `create_tenant`, `update_tenant`
- **Users:** `list_users`, `create_user`, `update_user`
- **Operations:** `dashboard_summary`, `list_jobs`, `get_job`, `retry_job`, `trigger_sync`, `stock_preview`, `not_found_skus`, `maestro_status`, `refresh_maestro`, `list_channels`
- **Products:** `create_products_shopify`, `create_products_marketplace`
- **Analytics:** `analytics_overview`, `sales_targets`
- **Agent tasks (conversational):** `agent_task_update`, `confirm_agent_task`
- **Generic:** `vago_api_request(method, path, ...)` for any endpoint not covered above

## Security

- The token acts as the user who issued it. Keep it secret.
- Use environment variables or local config files; never commit tokens.
- Revoke tokens immediately if they are leaked.

## License

MIT — see [LICENSE](./LICENSE).

## Links

- [VAGO Cloud](https://vagocloud.com)
- [Model Context Protocol](https://modelcontextprotocol.io)
