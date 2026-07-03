"""
vago_shopify.py
================
Herramientas de Shopify Admin para el MCP de VAGO Cloud, con selección de
tienda POR OPERACIÓN (equivalente a `shopify store execute --store ...`).

Filosofía (igual que el flujo de Cursor/CLI):
    - NO hay "tienda activa" global ni sesión OAuth única.
    - Cada llamada nombra la tienda con el parámetro `store`.
    - Los tokens viven en el backend de VAGO (ChannelConfig, cifrados en reposo)
      o en entorno como respaldo; jamás en el repo.

Integración en server.py:
    from vago_shopify import register_shopify_tools
    register_shopify_tools(mcp)          # mcp = instancia FastMCP existente

Requisitos: httpx  (pip install httpx)

Resolución de credenciales (resolve_store), en orden:
    1) ChannelConfig (fuente de verdad). Disponible cuando el server corre
       co-ubicado con el backend Django (mismo entorno/DB), p.ej. dentro del
       contenedor `backend` con DJANGO_SETTINGS_MODULE configurado. Lee
       `creds['shop']` + `creds['access_token']` exactamente como los persiste
       el callback OAuth (apps/channels/shopify_oauth_views.py).
    2) Respaldo por entorno (cuando no hay Django/DB a mano, p.ej. el MCP
       corriendo en la laptop apuntando a la API remota):
         VAGO_SHOPIFY_STORES   JSON: {"alias": {"domain": "...", "token": "..."}}
         SHOPIFY_DOMAIN_<ALIAS> / SHOPIFY_TOKEN_<ALIAS>

Variables de entorno:
    VAGO_SHOPIFY_API_VERSION       por defecto "2026-04"
    VAGO_SHOPIFY_ALLOW_MUTATIONS   "1" para habilitar escrituras (default: bloqueadas)
    VAGO_DJANGO_SETTINGS           (opcional) settings module para activar el
                                   modo ORM si Django no está ya inicializado.
                                   Default: 'config.settings'.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import httpx

_T = TypeVar("_T")

API_VERSION = os.getenv("VAGO_SHOPIFY_API_VERSION", "2026-04")
_ALLOW_MUTATIONS = os.getenv("VAGO_SHOPIFY_ALLOW_MUTATIONS", "0") == "1"

# Alias amigables -> dominio myshopify de respaldo. La fuente de verdad del
# dominio es ChannelConfig (creds['shop']); estos valores solo se usan en el
# modo respaldo-por-entorno y para documentar qué aliases son válidos.
#
# NOTA: 'mifelpa' NO es 'mifelpa.myshopify.com'. La tienda real de mifelpa es
# 'renzo-adrianzenm.myshopify.com' (ver scripts/mifelpa-theme/*: `shopify store
# execute --store renzo-adrianzenm.myshopify.com`). Corregido aquí.
KNOWN_ALIASES = {
    "vptrends": "vptrends.myshopify.com",
    "poptoons": "poptoonsculture.myshopify.com",
    "mifelpa": "renzo-adrianzenm.myshopify.com",
    "mundotec": "mundotec.myshopify.com",
}

# Canales de ChannelConfig que corresponden a tiendas Shopify Admin (una tienda
# Shopify "extra" del mismo tenant se persiste bajo el canal 'mundotec').
_SHOPIFY_CANALES = ("shopify", "mundotec")


@dataclass(frozen=True)
class StoreCreds:
    alias: str
    domain: str          # p.ej. vptrends.myshopify.com
    token: str           # Admin API access token (shpat_... o el del canal)

    @property
    def graphql_url(self) -> str:
        return f"https://{self.domain}/admin/api/{API_VERSION}/graphql.json"


class ShopifyError(RuntimeError):
    """Error de negocio/HTTP de Shopify, ya formateado para devolver al usuario."""


# ---------------------------------------------------------------------------
# Resolución de credenciales por tienda
# ---------------------------------------------------------------------------
def _ensure_django() -> bool:
    """
    Garantiza que Django esté inicializado para poder usar el ORM.

    Devuelve True si el ORM quedó disponible, False si no (sin lanzar): en ese
    caso resolve_store cae al respaldo por entorno. Es deliberadamente
    defensivo: el MCP también corre en máquinas sin Django/DB.
    """
    try:
        from django.conf import settings  # noqa: WPS433 (import local a propósito)
    except Exception:
        return False

    if settings.configured:
        return True

    settings_module = (
        os.getenv("VAGO_DJANGO_SETTINGS")
        or os.getenv("DJANGO_SETTINGS_MODULE")
        or "config.settings"
    )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    try:
        import django  # noqa: WPS433

        django.setup()
        return True
    except Exception:
        return False


def _in_async_context() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_sync_django(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """
    Ejecuta código sync Django/ORM desde contexto sync o async.

    Las tools del MCP son `async def` pero el ORM es sync; sin esto Django lanza
    SynchronousOnlyOperation y resolve_store caía silenciosamente al respaldo.
    """
    if not _in_async_context():
        return fn(*args, **kwargs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def _resolve_from_channelconfig(alias: str, key: str) -> StoreCreds | None:
    """
    Lee la tienda desde ChannelConfig (fuente de verdad).

    Empareja `alias`/`key` (alias amigable o dominio) contra cada ChannelConfig
    de canal Shopify activo, usando varias señales: dominio persistido
    (creds['shop']), su primer label, el nombre del tenant (slug) y el canal.
    Devuelve None si Django/DB no están disponibles o no hay match (resolve_store
    cae entonces al respaldo por entorno). Nunca loguea el token.
    """
    return _run_sync_django(_resolve_from_channelconfig_sync, alias, key)


def _resolve_from_channelconfig_sync(alias: str, key: str) -> StoreCreds | None:
    if not _ensure_django():
        return None

    try:
        from apps.channels.models import ChannelConfig  # noqa: WPS433
    except Exception:
        return None

    # Candidatos de dominio para este alias (incluye el dominio conocido).
    wanted = {alias, key}
    known_domain = KNOWN_ALIASES.get(alias)
    if known_domain:
        wanted.add(known_domain.lower())

    try:
        qs = (
            ChannelConfig.objects
            .filter(canal__in=_SHOPIFY_CANALES, activo=True)
            .select_related("tenant")
        )
        rows = list(qs)
    except Exception:
        return None

    for ch in rows:
        try:
            creds = ch.get_credenciales() or {}
        except Exception:
            continue
        shop = str(creds.get("shop") or "").strip().lower()
        token = str(creds.get("access_token") or "").strip()
        if not (shop and token):
            continue

        tenant_nombre = str(getattr(getattr(ch, "tenant", None), "nombre", "") or "").strip().lower()
        tenant_slug = _slug(tenant_nombre)
        signals = {
            shop,
            shop.split(".")[0],
            str(ch.canal or "").lower(),
            tenant_nombre,
            tenant_slug,
        }
        signals.discard("")

        if wanted & signals:
            # El dominio autoritativo es el persistido en ChannelConfig.
            return StoreCreds(alias=alias, domain=shop, token=token)

    return None


def _slug(value: str) -> str:
    return "".join(c for c in value.lower() if c.isalnum())


def _resolve_from_env(alias: str, key: str) -> StoreCreds | None:
    """Respaldo cuando no hay ChannelConfig/DB a mano."""
    # 1) Registro JSON completo
    raw = os.getenv("VAGO_SHOPIFY_STORES")
    if raw:
        try:
            registry = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ShopifyError(f"VAGO_SHOPIFY_STORES no es JSON válido: {e}") from e
        entry = registry.get(alias) or registry.get(key)
        if entry and entry.get("domain") and entry.get("token"):
            return StoreCreds(alias, entry["domain"], entry["token"])

    # 2) Variables sueltas por alias
    env_alias = alias.upper()
    domain = os.getenv(f"SHOPIFY_DOMAIN_{env_alias}") or KNOWN_ALIASES.get(alias)
    token = os.getenv(f"SHOPIFY_TOKEN_{env_alias}")
    if domain and token:
        return StoreCreds(alias, domain, token)

    return None


def resolve_store(store: str) -> StoreCreds:
    """
    Traduce un alias ('vptrends') o dominio ('vptrends.myshopify.com') a las
    credenciales de esa tienda.

    Orden de resolución:
      1) ChannelConfig (fuente de verdad; token+dominio cifrados como los
         persiste el OAuth callback). Requiere Django/DB accesibles.
      2) Respaldo por entorno (VAGO_SHOPIFY_STORES / SHOPIFY_*_<ALIAS>).
    """
    key = store.strip().lower()
    alias = key.split(".")[0] if key.endswith(".myshopify.com") else key

    creds = _resolve_from_channelconfig(alias, key)
    if creds is not None:
        return creds

    creds = _resolve_from_env(alias, key)
    if creds is not None:
        return creds

    disponibles = ", ".join(sorted(KNOWN_ALIASES)) or "(ninguna configurada)"
    env_alias = alias.upper()
    raise ShopifyError(
        f"No encuentro credenciales para la tienda '{store}'. "
        f"Verifica que el canal Shopify del tenant esté activo en ChannelConfig "
        f"(shop + access_token vía OAuth), o configura el respaldo por entorno "
        f"SHOPIFY_DOMAIN_{env_alias} y SHOPIFY_TOKEN_{env_alias} / "
        f"VAGO_SHOPIFY_STORES. Aliases conocidos: {disponibles}."
    )


def list_configured_stores() -> list[str]:
    """Aliases que tienen credenciales realmente resolubles ahora mismo."""
    out = []
    for alias in KNOWN_ALIASES:
        try:
            resolve_store(alias)
            out.append(alias)
        except ShopifyError:
            continue
    return out


# ---------------------------------------------------------------------------
# Cliente GraphQL con manejo de throttling
# ---------------------------------------------------------------------------
async def _execute(
    creds: StoreCreds,
    query: str,
    variables: dict[str, Any] | None,
    *,
    is_mutation: bool,
    max_retries: int = 4,
) -> dict[str, Any]:
    headers = {
        "X-Shopify-Access-Token": creds.token,
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            resp = await client.post(creds.graphql_url, json=payload, headers=headers)

            if resp.status_code == 429:  # rate limit a nivel REST
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise ShopifyError(
                    f"Token inválido o expirado para '{creds.alias}' "
                    f"({creds.domain}). Revisa ChannelConfig / el scope de la app."
                )
            resp.raise_for_status()
            body = resp.json()

            # Throttling a nivel GraphQL (Shopify devuelve THROTTLED en errors)
            errors = body.get("errors") or []
            throttled = any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors
            )
            if throttled and attempt < max_retries - 1:
                await asyncio.sleep(1.0 + attempt)
                continue
            if errors:
                msgs = "; ".join(e.get("message", str(e)) for e in errors)
                raise ShopifyError(f"GraphQL error en '{creds.alias}': {msgs}")

            return body.get("data", {})

    raise ShopifyError(f"Throttled por Shopify tras {max_retries} intentos ('{creds.alias}').")


def _check_user_errors(data: dict[str, Any], mutation_key: str) -> None:
    result = data.get(mutation_key) or {}
    user_errors = result.get("userErrors") or []
    if user_errors:
        msgs = "; ".join(
            f"{'.'.join(map(str, e.get('field') or []))}: {e.get('message')}"
            for e in user_errors
        )
        raise ShopifyError(f"userErrors en {mutation_key}: {msgs}")


# ---------------------------------------------------------------------------
# Registro de tools en el FastMCP existente
# ---------------------------------------------------------------------------
def register_shopify_tools(mcp) -> None:
    """Engancha las tools de Shopify al servidor FastMCP `mcp`."""

    @mcp.tool()
    async def shopify_list_stores() -> str:
        """Lista las tiendas Shopify con credenciales configuradas en este MCP."""
        stores = list_configured_stores()
        return json.dumps({"stores": stores}, ensure_ascii=False)

    @mcp.tool()
    async def shopify_admin_query(
        store: str, query: str, variables: dict | None = None
    ) -> str:
        """
        Ejecuta una query GraphQL READ-ONLY contra la tienda indicada.

        store: alias ('vptrends', 'poptoons') o dominio '<x>.myshopify.com'.
        query: query GraphQL del Admin API.
        variables: dict de variables (opcional).
        """
        creds = resolve_store(store)
        data = await _execute(creds, query, variables, is_mutation=False)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    async def shopify_admin_mutation(
        store: str, query: str, variables: dict | None = None
    ) -> str:
        """
        Ejecuta una MUTATION GraphQL (escritura) contra la tienda indicada.

        Bloqueada salvo que VAGO_SHOPIFY_ALLOW_MUTATIONS=1 (equivale a
        --allow-mutations del CLI). Esto evita escrituras accidentales en prod.
        """
        if not _ALLOW_MUTATIONS:
            raise ShopifyError(
                "Mutaciones deshabilitadas. Exporta VAGO_SHOPIFY_ALLOW_MUTATIONS=1 "
                "para permitir escrituras (igual que --allow-mutations)."
            )
        creds = resolve_store(store)
        data = await _execute(creds, query, variables, is_mutation=True)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    async def shopify_update_product_seo(
        store: str, product_id: str, seo_title: str, seo_description: str
    ) -> str:
        """
        Atajo de alto nivel: actualiza SOLO el SEO title y la meta description
        de un producto (campo `seo`), sin tocar nombre visible ni nada más.

        product_id: GID completo, p.ej. 'gid://shopify/Product/123'.
        """
        if not _ALLOW_MUTATIONS:
            raise ShopifyError(
                "Mutaciones deshabilitadas. Exporta VAGO_SHOPIFY_ALLOW_MUTATIONS=1."
            )
        if not product_id.startswith("gid://shopify/Product/"):
            raise ShopifyError("product_id debe ser un GID 'gid://shopify/Product/...'.")

        creds = resolve_store(store)
        mutation = """
        mutation actualizarSEO($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title seo { title description } }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "id": product_id,
                "seo": {"title": seo_title, "description": seo_description},
            }
        }
        data = await _execute(creds, mutation, variables, is_mutation=True)
        _check_user_errors(data, "productUpdate")
        return json.dumps(data["productUpdate"]["product"], ensure_ascii=False)
