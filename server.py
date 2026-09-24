"""
Cookidoo MCP Server

MCP tools for reading Cookidoo recipes and creating TM7-optimized custom recipes
with automatic quality validation. Runs as stateless HTTP server (token auth) or
over stdio for local Claude Desktop use.
"""

import json
import os
import re
import time
from typing import Optional

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from cookidoo_service import (
    CookidooService,
    build_step_annotations,
    load_cookidoo_credentials,
)
from schemas import CustomRecipe

VERSION = "0.2.0"
API_TOKEN = os.environ.get("COOKIDOO_API_TOKEN", "")
PORT = int(os.environ.get("COOKIDOO_MCP_PORT", "8001"))
QUALITY_BAR = int(os.environ.get("COOKIDOO_QUALITY_BAR", "70"))
TOKEN_REFRESH_BUFFER_SECONDS = 60

mcp = FastMCP("cookidoo-mcp-server")

_cookidoo_service: Optional[CookidooService] = None
_cookidoo_api = None


async def _ensure_connected() -> tuple[Optional[str], Optional[CookidooService]]:
    """Lazy-login with proactive token refresh.

    The server process is long-lived; Cookidoo access tokens are not. Without
    refresh, every authenticated call after the first hour returns 401. This
    function uses the OAuth refresh-grant via ``Cookidoo.refresh_token()`` so
    the email+password login only runs once per process (or after the refresh
    token itself dies).

    Returns ``(error_message, service)``. ``error_message`` is None on success.
    """
    global _cookidoo_service, _cookidoo_api
    if _cookidoo_service and _cookidoo_api:
        # cookidoo-api 0.18.x exposes the absolute expiry as ``_expires_at``
        # (a Unix timestamp); there is no ``expires_in`` attribute.
        seconds_left = getattr(_cookidoo_api, "_expires_at", 0.0) - time.time()
        if seconds_left > TOKEN_REFRESH_BUFFER_SECONDS:
            return None, _cookidoo_service
        try:
            await _cookidoo_api.refresh_token()
            return None, _cookidoo_service
        except Exception:
            try:
                await _cookidoo_service.close()
            except Exception:
                pass
            _cookidoo_service = None
            _cookidoo_api = None
    try:
        email, password = load_cookidoo_credentials()
        _cookidoo_service = CookidooService(email, password)
        _cookidoo_api = await _cookidoo_service.login()
        return None, _cookidoo_service
    except ValueError as e:
        return f"Configuration error: {e}. Ensure COOKIDOO_EMAIL and COOKIDOO_PASSWORD are set.", None
    except Exception as e:
        return f"Login failed: {e}", None


# -----------------------------------------------------------------------------
# Recipe quality validation (TM7-aware)
# -----------------------------------------------------------------------------

_ACCESSORY_RE = re.compile(
    r"(schmetterling|spatel|varoma|gareinsatz|messeinsatz|mixtopf|butterfly|simmering basket|mixing bowl)",
    re.IGNORECASE,
)
_PARALLEL_RE = re.compile(
    r"(gleichzeitig|parallel|während.*kocht|varoma.*aufsatz|varoma.*einsatz|während.*gart|meanwhile|while.*cook)",
    re.IGNORECASE,
)


def _score_recipe(recipe: CustomRecipe) -> dict:
    """
    Score a recipe for TM7 quality.

    Core criterion: how many action steps are parseable as TTS annotations, i.e.
    will render as tappable action chips with a play button on the TM7 device.
    Plus bonuses for ingredient annotation coverage, accessories, and TM7
    parallelization (Varoma / Gareinsatz).
    """
    steps = recipe.steps
    ingredients = recipe.ingredients
    n = len(steps)
    if n == 0:
        return {
            "score": 0, "meets_bar": False,
            "issues": ["Recipe has no steps."],
            "suggestions": [],
            "breakdown": {},
        }

    issues: list[str] = []
    suggestions: list[str] = []

    # Parse each step and count action hits (TTS or MODE — both render with play button)
    tts_step_count = 0
    ingredient_hit_steps = 0
    for step in steps:
        anns = build_step_annotations(step, ingredients)
        if any(a["type"] in ("TTS", "MODE") for a in anns):
            tts_step_count += 1
        if any(a["type"] == "INGREDIENT" for a in anns):
            ingredient_hit_steps += 1

    # Estimate how many action-steps the recipe SHOULD have: roughly half the
    # steps should be actions (alternating prose/action pattern).
    expected_actions = max(1, n // 2)

    has_accessory = any(_ACCESSORY_RE.search(s) for s in steps)
    has_parallel = any(_PARALLEL_RE.search(s) for s in steps)
    has_varoma = any(re.search(r"varoma", s, re.IGNORECASE) for s in steps)
    has_butterfly = any(
        re.search(r"schmetterling|butterfly", s, re.IGNORECASE) for s in steps
    )

    # Points 1: TTS-parseable action steps (50 pts — the big one)
    tts_ratio = min(1.0, tts_step_count / expected_actions)
    tts_points = round(tts_ratio * 50)
    if tts_step_count == 0:
        issues.append(
            "Kein Schritt hat eine parseable TTS-Aktion. Formuliere Maschinen-Aktionen "
            "in einem eigenen Schritt, z.B. 'Zerkleinern 5 Sek./Stufe 5.' oder "
            "'Kochen 18 Min./100°C/Linkslauf/Stufe 1.' — nur diese werden mit "
            "Play-Button am TM7 als guided step angezeigt."
        )
    elif tts_ratio < 0.5:
        issues.append(
            f"Nur {tts_step_count} von ~{expected_actions} erwarteten Aktionsschritten "
            "sind TTS-parseable. Weitere Aktionen im Format 'Zeit/[Temp°C/][Linkslauf/]Stufe X' "
            "in eigenen Schritten ergänzen."
        )

    # Points 2: ingredient references resolved via annotations (20 pts)
    ing_points = 20 if ingredient_hit_steps >= 1 else 0
    if ingredient_hit_steps == 0 and ingredients:
        issues.append(
            "Keine Zutat wird im Schritt-Text exakt referenziert. Verwende die "
            "Zutaten-Einträge 1:1 im Text (z.B. '200 g Langkornreis in den Mixtopf geben.')"
            " damit sie als INGREDIENT-Annotation verlinkt werden."
        )

    # Points 3: accessories mentioned (10 pts)
    accessory_points = 10 if has_accessory else 0
    if not has_accessory:
        suggestions.append(
            "Erwäge Zubehör zu nennen: Schmetterling, Spatel, Varoma-Aufsatz, Gareinsatz."
        )

    # Points 4: TM7 parallelization (20 pts — Varoma/Gareinsatz)
    parallel_points = 0
    if has_parallel:
        parallel_points += 10
    if has_varoma:
        parallel_points += 10
    parallel_points = min(20, parallel_points)
    if parallel_points < 20:
        suggestions.append(
            "TM7-Parallelisierung: prüfe ob Varoma-Aufsatz (Dämpfen oben) oder "
            "Gareinsatz (Pasta/Reis im Mixtopf) parallel zum Haupt-Kochen genutzt werden "
            "kann — spart Zeit und ist TM7 best practice."
        )

    score = tts_points + ing_points + accessory_points + parallel_points

    text_all = " ".join(steps).lower()
    if ("sahne" in text_all or "eiweiß" in text_all or "eischnee" in text_all or "cream" in text_all) and not has_butterfly:
        suggestions.append("Bei Sahne/Eischnee: Schmetterling einsetzen.")
    if ("teig" in text_all or "dough" in text_all) and not re.search(
        r"teigknetstufe|kneading", text_all, re.IGNORECASE
    ):
        suggestions.append("Bei Teig: Teigknetstufe (🌾) verwenden.")

    meets_bar = score >= QUALITY_BAR

    return {
        "score": score,
        "meets_bar": meets_bar,
        "issues": issues,
        "suggestions": suggestions,
        "breakdown": {
            "tts_points": tts_points,
            "ingredient_points": ing_points,
            "accessory_points": accessory_points,
            "parallel_points": parallel_points,
            "tts_step_count": tts_step_count,
            "ingredient_hit_steps": ingredient_hit_steps,
            "expected_actions": expected_actions,
            "quality_bar": QUALITY_BAR,
        },
    }


# -----------------------------------------------------------------------------
# Tools
# -----------------------------------------------------------------------------


@mcp.tool()
async def connect_to_cookidoo() -> str:
    """
    Authenticate with Cookidoo. Usually not needed — other tools auto-connect.
    Only call this explicitly if you want to verify credentials up front.
    """
    error, _ = await _ensure_connected()
    if error:
        return error
    email, _ = load_cookidoo_credentials()
    return f"Connected to Cookidoo as {email}"


@mcp.tool()
async def get_recipe_details(recipe_id: str) -> str:
    """
    Fetch a Cookidoo recipe by ID (e.g. 'r59322', 'r907015').

    Use this to study existing recipes for structure, times, temperatures, and
    speed settings before drafting your own. Auto-connects if needed.
    """
    error, _ = await _ensure_connected()
    if error:
        return error
    try:
        recipe = await _cookidoo_api.get_recipe_details(recipe_id)
    except Exception as e:
        return f"Failed to get recipe details: {e}"

    result = f"Recipe Details:\n\nName: {getattr(recipe, 'name', '?')}\nID: {getattr(recipe, 'id', recipe_id)}\n\n"
    if hasattr(recipe, "serving_size"):
        result += f"Servings: {recipe.serving_size}\n"
    if hasattr(recipe, "total_time"):
        result += f"Total Time: {recipe.total_time} minutes\n"
    if hasattr(recipe, "difficulty"):
        result += f"Difficulty: {recipe.difficulty}\n"
    result += "\n"

    if getattr(recipe, "ingredients", None):
        result += "Ingredients:\n"
        for ing in recipe.ingredients:
            name = getattr(ing, "name", str(ing))
            qty = getattr(ing, "quantity", None)
            result += f"  • {name}" + (f" — {qty}" if qty else "") + "\n"
        result += "\n"

    if getattr(recipe, "steps", None):
        result += "Steps:\n"
        for i, step in enumerate(recipe.steps, 1):
            desc = getattr(step, "description", str(step))
            result += f"{i}. {desc}\n"
        result += "\n"

    if getattr(recipe, "url", None):
        result += f"URL: {recipe.url}\n"
    return result


@mcp.tool()
async def generate_recipe_structure(
    name: str,
    ingredients: str,
    steps: str,
    servings: int = 4,
    prep_time: int = 30,
    total_time: int = 60,
    hints: str = "",
) -> str:
    """
    Validate recipe data and return a JSON structure ready for quality check + upload.

    Steps should use Thermomix vocabulary (time/temperature/speed) and ideally
    leverage TM7 parallelization (Varoma, Gareinsatz). Ingredients and steps can
    be newline- or comma-separated.
    """
    try:
        ingredients_list = [
            ing.strip()
            for ing in (ingredients.split("\n") if "\n" in ingredients else ingredients.split(","))
            if ing.strip()
        ]
        steps_list = [
            step.strip().lstrip("0123456789.)-• \t")
            for step in steps.split("\n")
            if step.strip()
        ]
        hints_list = None
        if hints:
            hints_list = [
                h.strip()
                for h in (hints.split("\n") if "\n" in hints else hints.split(","))
                if h.strip()
            ]
        recipe = CustomRecipe(
            name=name,
            ingredients=ingredients_list,
            steps=steps_list,
            servings=servings,
            prep_time=prep_time,
            total_time=total_time,
            hints=hints_list,
        )
        return (
            "Recipe structure validated.\n\n"
            + recipe.model_dump_json(indent=2)
            + "\n\nNext: call validate_recipe_quality(recipe_json) to check TM7 quality, "
            "then upload_custom_recipe(recipe_json) to publish."
        )
    except Exception as e:
        return f"Validation failed: {e}"


@mcp.tool()
async def validate_recipe_quality(recipe_json: str) -> str:
    """
    Check a recipe against TM7 quality criteria.

    Returns a score 0-100, whether it meets the quality bar, specific issues
    (missing time/temp/speed), and suggestions for TM7 parallelization and
    best-practice cooking. Use this before upload_custom_recipe. If score is
    below the bar, revise the steps and re-validate.
    """
    try:
        data = json.loads(recipe_json)
        recipe = CustomRecipe(**data)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    except Exception as e:
        return f"Invalid recipe data: {e}"

    result = _score_recipe(recipe)
    bd = result["breakdown"]
    lines = [
        f"TM7 Quality Score: {result['score']}/100 (bar: {QUALITY_BAR})",
        f"Meets quality bar: {'YES ✓' if result['meets_bar'] else 'NO — revise before upload'}",
        "",
        "Breakdown:",
        f"  TTS action steps (play button): {bd['tts_points']}/50  "
        f"({bd['tts_step_count']} parseable / ~{bd['expected_actions']} expected)",
        f"  Ingredient annotations:         {bd['ingredient_points']}/20  "
        f"({bd['ingredient_hit_steps']} step(s) with linked ingredients)",
        f"  Accessories mentioned:          {bd['accessory_points']}/10",
        f"  TM7 parallelization:            {bd['parallel_points']}/20",
    ]
    if result["issues"]:
        lines.append("")
        lines.append("Issues (fix these to meet the bar):")
        for issue in result["issues"]:
            lines.append(f"  ✗ {issue}")
    if result["suggestions"]:
        lines.append("")
        lines.append("Suggestions (optional improvements):")
        for s in result["suggestions"]:
            lines.append(f"  • {s}")
    return "\n".join(lines)


@mcp.tool()
async def list_my_custom_recipes() -> str:
    """
    List all custom recipes in the user's Cookidoo account.

    Use this to find recipes by name (e.g. before deleting or when looking up
    a prior upload). Returns name, recipe_id, creation date, total_time, and
    servings for each entry.
    """
    error, service = await _ensure_connected()
    if error:
        return error
    try:
        items = await service.list_custom_recipes()
    except Exception as e:
        return f"Failed to list recipes: {e}"
    if not items:
        return "No custom recipes in account."
    lines = [f"{len(items)} custom recipe(s) in account:\n"]
    for it in items:
        lines.append(
            f"  {it['recipe_id']}  {it['name']}  "
            f"(created {it.get('created_at', '?')}, "
            f"{it.get('servings', '?')} portions, {it.get('total_time', '?')})"
        )
    return "\n".join(lines)


@mcp.tool()
async def attach_recipe_image(recipe_id: str, image_path: str) -> str:
    """
    Attach a local image file to a custom recipe as its recipe photo.

    Uploads the image to Cookidoo's Cloudinary backend (server-signed) and sets
    it on the recipe, so it shows up on the recipe card and on the TM7. Give the
    recipe_id (from upload_custom_recipe or list_my_custom_recipes) and an
    absolute path to a JPEG/PNG on disk. Auto-connects if needed.
    """
    error, service = await _ensure_connected()
    if error:
        return error
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        return f"Could not read image file: {e}"
    try:
        image_ref = await service.upload_recipe_image(recipe_id, image_bytes)
        return f"Image attached to recipe {recipe_id} (public_id: {image_ref})."
    except Exception as e:
        return f"Failed to attach image: {e}"


@mcp.tool()
async def delete_custom_recipe(recipe_id: str) -> str:
    """
    Permanently delete a custom recipe from the user's Cookidoo account.

    Use this to clean up failed/zombie uploads or old test recipes. Only custom
    (user-created) recipes can be deleted — official Cookidoo content cannot.
    Irreversible; ask the user for confirmation before calling.
    """
    error, service = await _ensure_connected()
    if error:
        return error
    try:
        await service.delete_custom_recipe(recipe_id)
        return f"Deleted custom recipe {recipe_id}."
    except Exception as e:
        return f"Failed to delete recipe: {e}"


@mcp.tool()
async def upload_custom_recipe(recipe_json: str, force_upload: bool = False) -> str:
    """
    Upload a recipe to the user's Cookidoo account.

    Automatically validates TM7 quality first and refuses upload if the recipe
    does not meet the quality bar. Set force_upload=True only if the user has
    explicitly accepted a lower-quality upload. Auto-connects if needed.
    """
    error, service = await _ensure_connected()
    if error:
        return error

    try:
        data = json.loads(recipe_json)
        recipe = CustomRecipe(**data)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    except Exception as e:
        return f"Invalid recipe data: {e}"

    quality = _score_recipe(recipe)
    if not quality["meets_bar"] and not force_upload:
        lines = [
            f"Upload blocked — quality score {quality['score']}/100 below bar {QUALITY_BAR}.",
            "",
            "Issues:",
        ]
        for issue in quality["issues"]:
            lines.append(f"  ✗ {issue}")
        if quality["suggestions"]:
            lines.append("")
            lines.append("Suggestions:")
            for s in quality["suggestions"]:
                lines.append(f"  • {s}")
        lines.append("")
        lines.append(
            "Revise the steps with Thermomix vocabulary and TM7 parallelization, "
            "then call validate_recipe_quality again. To override, pass force_upload=true."
        )
        return "\n".join(lines)

    try:
        recipe_id = await service.create_custom_recipe(
            name=recipe.name,
            ingredients=recipe.ingredients,
            steps=recipe.steps,
            servings=recipe.servings,
            prep_time=recipe.prep_time,
            total_time=recipe.total_time,
            hints=recipe.hints,
            tools=recipe.tools,
        )
        from urllib.parse import urlparse
        localization = _cookidoo_api.localization
        parsed = urlparse(localization.url)
        recipe_url = (
            f"{parsed.scheme}://{parsed.netloc}/created-recipes/"
            f"{localization.language}/{recipe_id}/edit"
        )
        return (
            f"Recipe '{recipe.name}' created (quality {quality['score']}/100).\n\n"
            f"Recipe ID: {recipe_id}\nURL: {recipe_url}"
        )
    except Exception as e:
        return f"Upload failed: {e}"


# -----------------------------------------------------------------------------
# Prompt — autonomous TM7 recipe creation workflow
# -----------------------------------------------------------------------------


@mcp.prompt()
def create_tm7_recipe(dish: str) -> str:
    """Autonomous workflow to create and upload a TM7-optimized custom recipe with guided-cooking annotations."""
    return f"""Du bist ein erfahrener Thermomix-Koch und erstellst ein TM7-optimiertes Rezept für: **{dish}**

Arbeite autonom durch diesen Workflow, nur die Endbestätigung hole dir vom User ein.

## 1. Inspiration (optional)
Wenn ein Referenz-Rezept bekannt ist, hol es mit `get_recipe_details` und analysiere Struktur/Zeiten/Temperaturen.

## 2. Step-Format (KRITISCH — sonst kein Play-Button am TM7)

Der Cookidoo-Backend erkennt TM-Aktionen nur wenn sie in einem **eigenen Schritt** stehen. Der Parser sucht nach diesem Pattern:

```
ZEIT/[TEMP°C/][Linkslauf/]Stufe X
```

### Unterstützte Action-Formate (mit Play-Button)

**1. Standard-Kochen / Mixen (TTS)**
```
X Sek./Stufe Y                              (reine Geschwindigkeit)
X Min./T°C/Stufe Y                          (mit Temperatur)
X Min./T°C/Linkslauf/Stufe Y                (mit Temperatur und Richtung)
```
Beispiele: `5 Sek./Stufe 5`, `3 Min./120°C/Linkslauf/Stufe 1`, `18 Min./100°C/Linkslauf/Stufe 1`

Temperatur-Werte (°C) sind nur in diskreten Schritten erlaubt: `37, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 98, 100, 105, 110, 115, 120`. Andere Werte werden vom Backend abgelehnt → Upload schlägt fehl.

**2. Varoma-Dämpfen (MODE/STEAMING)**
```
X Min./Varoma/Stufe Y
```
Beispiel: `15 Min./Varoma/Stufe 2`

Varoma ist ein Dampf-Modus, **keine Temperatur-Zahl**. Wenn du mit dem Varoma-Aufsatz (oben drauf) dämpfst, schreib `Varoma` — NICHT `100°C` oder `120°C`. Die beiden Modi sind am Gerät unterschiedlich.

**3. Anbraten / Browning (MODE/BROWNING)**
```
X Min./T°C/Leistung
```
Beispiele: `7 Min./160°C/Intensiv`, `10 Min./140°C/Leicht`

Nur diese 5 Temperaturen erlaubt: `140, 145, 150, 155, 160` °C. Max 30 Min Zeit. Leistung ist entweder `Leicht` oder `Intensiv`.

### Normalisierung — lass den Verb-Prefix weg oder nicht, egal

Der Parser strippt automatisch einen Verb-Prefix wie `Mahlen`, `Zerkleinern`, `Dämpfen`, `Kochen`, `Anbraten` und trailende Satzzeichen. Das heißt: `"Mahlen 30 Sek./Stufe 10."` und `"30 Sek./Stufe 10"` werden beide identisch gespeichert. **Wichtig**: der Schritt darf **KEINE weitere Prosa** enthalten — keine Erklärung, kein Kontext, nur das Action-Pattern (+ optional einfacher Verb-Prefix). Prosa um eine Action herum führt am TM7 zur Checkbox-Anzeige statt Play-Button. Erklärungen gehören in den **vorherigen Schritt**.

- ✓ RICHTIG: `"Mahlen 30 Sek./Stufe 10."` (verb + action)
- ✓ RICHTIG: `"30 Sek./Stufe 10"` (pure action)
- ✗ FALSCH: `"Nach der Hälfte wenden, dann 30 Sek./Stufe 10 weiterlaufen lassen."` (prose um action → Checkbox)

### Nicht unterstützte Modi (als Prosa schreiben)

Diese TM7-Modi sind noch nicht im Parser — kein Play-Button möglich. Schreib sie als normalen Prosa-Schritt, der User stellt am TM7 manuell ein:

- **Modus Gären / Fermentieren** (WARM_UP, 37–90°C)
- **Modus Rice Cooker**
- **Modus Turbo / Mixen kräftig**
- **Teigknetstufe 🌾** (DOUGH mode, nur Time-Parameter)

### Die drei harten Regeln

**Regel 1: Maschinen-Aktion in eigenem Schritt.** Kein Vermischen mit Prosa.
- ✗ FALSCH: `"Zwiebel und Knoblauch zugeben und 5 Sek./Stufe 5 zerkleinern."`
- ✓ RICHTIG: Schritt N `"Zwiebel und Knoblauch in den Mixtopf geben."`, Schritt N+1 `"Zerkleinern 5 Sek./Stufe 5."`

**Regel 2: Zutaten im vorherigen Schritt hinzufügen, Action danach.**
- Schritt A: alle Zutaten die du einfüllst — als Prosa, mit exakten Mengen
- Schritt B: die Maschinen-Aktion allein

**Regel 3: Zutaten-Referenzen müssen 1:1 zur Zutaten-Liste passen.** Der Parser matcht per exact substring. Wenn die Zutat `"200 g Langkornreis"` heißt, schreib im Step-Text ebenfalls `"200 g Langkornreis"` — nicht `"der Reis"`, nicht `"200g Langkornreis"` (Leerzeichen!), nicht `"200 g Reis"`.

### Units / Vokabular (deutsch)
- Zeit: `Sek.` oder `Min.` (Punkt!)
- Trenner: `/` (forward slash)
- Geschwindigkeit: `Stufe X` (ganze Zahlen 1–10, oder `0.5` etc.)
- Temperatur: `X°C` oder `Varoma`
- Richtung: `Linkslauf` (optional, steht vor Stufe)

## 3. TM7-Parallelisierung aktiv einplanen
- Varoma-Aufsatz: Gemüse/Fisch/Knödel dämpfen während im Mixtopf gekocht/gerührt wird
- Gareinsatz: Pasta/Reis/Kartoffeln im Mixtopf-Inneren kochen gleichzeitig zum Hauptprozess
- Mise en place parallel zu Aufheizphasen

## 4. Best-Practice Koch-Technik
- Zwiebel/Knoblauch/Kräuter zuerst hacken (Stufe 5–7, 3–5 Sek.), dann beiseite oder weiter im Topf
- Linkslauf + Stufe 1–2 für empfindliche Zutaten die nicht zerkleinert werden sollen
- Schmetterling für Sahne, Eischnee, Butter aufschlagen
- Teigknetstufe 🌾 für jeden Teig
- Mise en place direkt im Mixtopf wiegen (spart Geschirr)

## 5. Workflow

1. `generate_recipe_structure(name, ingredients, steps, servings, prep_time, total_time, hints)` — parse + validate Schema
2. `validate_recipe_quality(recipe_json)` — schauen ob Score ≥ {QUALITY_BAR}; besonders wichtig: `tts_step_count` soll > 0 sein, sonst keine Play-Buttons
3. Falls Score zu niedrig oder `tts_step_count = 0`: Schritte umstrukturieren nach Regel 1+2, erneut validieren
4. Finale JSON + Score dem User zeigen, **einmal** "Hochladen?" fragen
5. Bei Freigabe: `upload_custom_recipe(recipe_json)` → URL zurückgeben
"""


# -----------------------------------------------------------------------------
# HTTP transport (token auth + health) — used when run as a server process
# -----------------------------------------------------------------------------


class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path == "/health":
            return await call_next(request)
        if API_TOKEN:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {API_TOKEN}":
                return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


async def health_endpoint(request):
    return JSONResponse(
        {
            "status": "ok",
            "service": "cookidoo-mcp-server",
            "version": VERSION,
            "quality_bar": QUALITY_BAR,
            "auth_configured": bool(API_TOKEN),
        }
    )


def build_http_app():
    app = mcp.http_app(transport="streamable-http", stateless_http=True, json_response=True)
    app.add_middleware(TokenAuthMiddleware)
    app.routes.append(Route("/health", health_endpoint))
    return app


app = None
if os.environ.get("COOKIDOO_MCP_MODE", "").lower() == "http":
    app = build_http_app()


if __name__ == "__main__":
    mode = os.environ.get("COOKIDOO_MCP_MODE", "http").lower()
    if mode == "stdio":
        mcp.run()
    else:
        import uvicorn

        if app is None:
            app = build_http_app()
        uvicorn.run(app, host="0.0.0.0", port=PORT)
