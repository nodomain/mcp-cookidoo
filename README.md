# Cookidoo MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io/)

Let Claude read recipes from your Cookidoo account and create new TM7-optimized custom recipes with guided-cooking annotations, quality scoring, and automatic rollback on upload failure.

> **Disclaimer:** Unofficial project. Not affiliated with Cookidoo, Vorwerk, Thermomix or any of their subsidiaries. Cookidoo has no public API, so this server uses the [`cookidoo-api`](https://github.com/miaucl/cookidoo-api) scraping/auth library. Use at your own risk and review the Cookidoo Terms of Service.

## Quickstart (Claude Desktop)

1. Clone and install:
   ```bash
   git clone https://github.com/danielkliem/mcp-cookidoo.git
   cd mcp-cookidoo
   python3.12 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "cookidoo": {
         "command": "/absolute/path/to/mcp-cookidoo/venv/bin/python",
         "args": ["/absolute/path/to/mcp-cookidoo/server.py"],
         "env": {
           "COOKIDOO_MCP_MODE": "stdio",
           "COOKIDOO_EMAIL": "you@example.com",
           "COOKIDOO_PASSWORD": "your-password"
         }
       }
     }
   }
   ```

3. Restart Claude Desktop. Ask Claude: *"Create a TM7 recipe for Spaghetti Carbonara and upload it to my Cookidoo."*

> **Credentials warning:** Cookidoo has no OAuth, so the server logs in with your email + password. Run it locally (stdio mode) unless you control the host. The HTTP transport supports a Bearer token but does not encrypt credentials at rest beyond your `.env` file permissions.

## What it does

- **Read recipes.** Fetch any Cookidoo recipe by ID (e.g. `r59322`) including ingredients, steps, timings, and metadata.
- **Create TM7-optimized custom recipes.** Action steps are parsed and rewritten into the three guided-cooking annotation families:
  - **TTS** (standard): `30 Sek./Stufe 5`
  - **MODE/STEAMING** (Varoma): auto-detects the word "Varoma" and emits the correct accessory shape
  - **MODE/BROWNING** (Anbraten): `5 Min./150°C/Intensiv`
  
  These render as proper guided steps with a Play-Button on the TM7 device. Verb prefixes like *Mahlen*, *Zerkleinern*, *Anbraten* are auto-stripped, since pure-action steps render as guided steps while verb-prefixed prose renders as "mark as done" checkboxes.
- **Quality gate.** Recipes are scored 0–100 on Thermomix vocabulary coverage before upload. Below the configured bar, upload is refused unless `force_upload=true`.
- **Automatic rollback.** If the upload PATCH fails (e.g. schema validation error), the partial recipe is deleted so no zombies accumulate in your account.
- **Guided workflow prompt.** The MCP prompt `create_tm7_recipe(dish)` carries the full reverse-engineered step grammar — supported action formats, allowed discrete temperature values, and the structuring rules — so the model learns the constraints before writing a single step.

## MCP Tools

| Tool | Description |
|------|-------------|
| `connect_to_cookidoo()` | Verify credentials. Optional, other tools auto-connect. |
| `get_recipe_details(recipe_id)` | Fetch an existing Cookidoo recipe by ID. |
| `generate_recipe_structure(...)` | Parse + validate recipe data into the upload JSON schema. |
| `validate_recipe_quality(recipe_json)` | Score recipe against TM7 criteria with suggestions. |
| `upload_custom_recipe(recipe_json, force_upload=false)` | Upload to the user's account. Quality-gated, with rollback on failure. |
| `update_custom_recipe(recipe_id, recipe_json, force_upload=false)` | Update an existing recipe in place, preserving its ID and photo. Quality-gated. |
| `list_my_custom_recipes()` | List custom recipes in the account. |
| `attach_recipe_image(recipe_id, image_path)` | Upload a local image and set it as the recipe photo (shows on the card and TM7). |
| `delete_custom_recipe(recipe_id)` | Delete a custom recipe. Irreversible. |

## MCP Prompts

- `create_tm7_recipe(dish)`: autonomous workflow that drives Claude through TM7-optimized recipe creation, from concept to validated upload.

## Quality Scoring

Score out of 100. Default upload threshold is 70, configurable via `COOKIDOO_QUALITY_BAR`. The score is dominated by how many steps are parseable as TM7 *guided-cooking* actions — only those render with a Play-Button on the device.

| Criterion | Points | Notes |
|-----------|--------|-------|
| TTS/MODE-parseable action steps (Play-Button) | 50 | Roughly half of steps should be pure actions like `5 Sek./Stufe 5` or `15 Min./Varoma/Stufe 2`. Scales linearly with coverage. |
| Ingredient annotations resolved in step text | 20 | At least one step should reference an ingredient using the exact substring from the ingredient list, so the backend can link them. |
| Accessory mentions (Schmetterling, Varoma, Spatel, Gareinsatz, …) | 10 | Helps the cook know what to attach. |
| TM7 parallelization (Varoma above, Gareinsatz inside, "gleichzeitig"/"während") | 20 | Encourages using the TM7's two cooking zones at once. |

The validator also produces contextual suggestions, e.g. Schmetterling for cream/egg whites, Teigknetstufe for dough.

The stylistic criteria together max out at 50 points, so the default bar of 70 cannot be met without a substantial share of parseable guided actions — the one criterion that is functional rather than cosmetic.

## Design Decisions

**Auth is a precondition, not a step.** Every tool calls `_ensure_connected()` itself instead of requiring the model to call `connect_to_cookidoo` first. A model planning a multi-tool workflow will skip a setup step it does not consider load-bearing, and the resulting auth error is an unhelpful place to fail. The explicit tool remains for credential debugging. The cost is a module-level session singleton — correct for a single-account server, and the reason this is not multi-tenant.

**The quality gate blocks rather than warns.** An upload is side-effectful and lands in a real account, and the score measures function, not taste: a low score means the recipe will render without play buttons, which is the entire point of the project. A warning in a tool result is advisory text the model may reason past, so the refusal is enforced in code, with `force_upload=true` as the explicit user-accepted override.

**Failed uploads roll back instead of retrying.** Creation is two calls: a POST that creates a named empty recipe and a PATCH that fills it. PATCH failures are almost always schema rejections — an out-of-enum temperature, a malformed annotation — which are deterministic, so a retry would send the same rejected payload again. Instead the orphaned recipe is deleted and the original error surfaced. The rollback is best-effort and deliberately swallows its own exceptions: a failing cleanup must not mask the error that caused it.

**Ingredient matching is exact-substring, longest-first.** Fuzzy matching would link "Reis" to "200 g Langkornreis" and produce annotations whose character offsets do not match what the user sees. Instead the matcher requires the step text to repeat the ingredient entry verbatim, resolves longer entries first, and skips spans that overlap an already-claimed region, so an ingredient inside an action span is not double-annotated. The strictness is pushed onto the model, which the MCP prompt states as a hard rule.

**Both transports, one tool implementation.** stdio serves local Claude Desktop; stateless HTTP with a bearer token serves remote connectors, which hold no session between requests. A single environment variable selects the mode, and the tool functions are unaware of either.

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Default | Purpose |
|----------|---------|---------|
| `COOKIDOO_EMAIL` | required | Cookidoo account email |
| `COOKIDOO_PASSWORD` | required | Cookidoo account password |
| `COOKIDOO_MCP_MODE` | `stdio` | `stdio` for Claude Desktop, `http` for remote |
| `COOKIDOO_MCP_PORT` | `8001` | Local bind port (HTTP mode only) |
| `COOKIDOO_API_TOKEN` | empty | Bearer token required on `/mcp`. Empty = no middleware auth. |
| `COOKIDOO_QUALITY_BAR` | `70` | Minimum quality score required by `upload_custom_recipe` |
| `COOKIDOO_INSECURE_SSL` | off | Set to `1` to disable TLS verification (debugging behind intercepting proxies only) |

## HTTP Transport

For remote use (e.g. claude.ai connectors) run the server in HTTP mode:

```bash
COOKIDOO_MCP_MODE=http \
COOKIDOO_API_TOKEN=$(openssl rand -hex 32) \
./venv/bin/python server.py
```

Endpoints:
- `POST /mcp`: MCP protocol endpoint, requires `Authorization: Bearer <COOKIDOO_API_TOKEN>` if the token is set.
- `GET /health`: health check.

For production deployment behind a reverse proxy (Caddy/nginx/Traefik), terminate TLS at the proxy and forward to `localhost:8001`. A typical Caddy config injects the Bearer token server-side so the URL itself carries a path token, keeping credentials out of client config.

## Troubleshooting

- **401 after ~1 hour of uptime.** Fixed in [`5f85973`](https://github.com/danielkliem/mcp-cookidoo/commit/5f85973). The server now refreshes the OAuth token automatically before expiry. If you still see 401, update to latest.
- **Upload returns 404 on the response URL.** Make sure you're on a version that points at `/created-recipes/{locale}/{id}/edit` rather than the old `/recipes/custom-recipes/...` path.
- **Zombie empty recipes in your account.** Should not happen, since rollback runs automatically if the PATCH step fails. Delete leftovers with `delete_custom_recipe` or `list_my_custom_recipes`.
- **Step renders with "mark as done" checkbox instead of Play-Button.** The verb prefix wasn't stripped. Ensure the step text starts with a quantity (e.g. `30 Sek./Stufe 5`), not a verb (`Mahlen 30 Sek./Stufe 5`).

## Known Limitations

- **Single account.** The authenticated session is a module-level singleton, so one running instance serves exactly one Cookidoo account.
- **Undocumented API.** The created-recipes endpoint and its annotation schema were reverse-engineered from the web app and can change without warning.
- **German vocabulary first.** The parser accepts some English and French tokens, but the scorer's heuristics and the MCP prompt are written for `de-CH`.
- **Not all TM7 modes are covered.** Gären/Fermentieren, Rice Cooker, Turbo and Teigknetstufe have no annotation support yet; the prompt instructs the model to write them as prose so the user sets them manually on the device.
- **Shopping list ingredients land under "Sonstige" (Misc).** When a custom recipe is added to the Cookidoo shopping list, every ingredient is categorized as `ShoppingCategory-rpf-10` (Sonstige), regardless of what it is. This is a Cookidoo backend behaviour, not an MCP limitation: the public custom-recipe API accepts ingredients only as free-text strings, the Cookidoo web editor itself has no per-ingredient category picker or canonical-ingredient autocomplete, and the backend hardcodes the `rpf-10` reference for every customer-recipe ingredient. Categorization on the shopping list works for native Cookidoo recipes because those carry a canonical `ingredient_ref` that maps to a category server-side. There is currently no known workaround.

## Development

```bash
pip install -r requirements.txt
python -m pytest test_integration.py test_auth_refresh.py
```

`test_integration.py` creates a real recipe in the configured account, asserts backend rendering, and cleans up. Requires valid `.env` credentials.

## Acknowledgments

- [cookidoo-api](https://github.com/miaucl/cookidoo-api) by miaucl for the underlying Cookidoo client library.
- [FastMCP](https://github.com/jlowin/fastmcp) for the MCP server framework.

## License

MIT. See [LICENSE](LICENSE).
