"""
VAGO Cloud MCP server — conecta agentes IA (Claude Code, Cursor, Cowork via
mcp-remote) con la API de VAGO Cloud usando un token de agente.

Transporte: stdio (lo lanza el cliente MCP).

Variables de entorno:
- VAGO_AGENT_TOKEN  (requerida)  token `vago_agt_...` emitido en
                                  Gestión de cuenta → Agentes IA (o
                                  `manage.py create_agent_token`).
- VAGO_API_URL      (opcional)   default: https://www.vagocloud.com
- VAGO_TENANT_ID    (opcional)   tenant por defecto para los headers
                                  X-Tenant-Id; cada tool acepta tenant_id
                                  explícito que lo sobreescribe.

El token actúa como el usuario que lo emitió (normalmente superadmin):
las tools tienen el mismo poder que ese usuario en la app. El switch de
emergencia en Gestión de cuenta → Agentes IA corta todo el acceso.
"""

import json
import os
import sys
from typing import Any

# Cuando el server corre co-ubicado con el backend (imagen Docker copia
# tools/vago-mcp a /app/vago-mcp), `python /app/vago-mcp/server.py` deja en
# sys.path[0] a /app/vago-mcp, NO a /app. Sin /app en el path, Django no puede
# importar `config.settings` ni `apps.channels.models`, y resolve_store cae en
# silencio al respaldo por entorno (0 tiendas). Si el directorio padre es la
# raíz del proyecto Django (tiene manage.py), lo agregamos al path.
_DJANGO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.exists(os.path.join(_DJANGO_ROOT, 'manage.py')) and _DJANGO_ROOT not in sys.path:
    sys.path.insert(0, _DJANGO_ROOT)

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

API_URL = os.environ.get('VAGO_API_URL', 'https://www.vagocloud.com').rstrip('/')
TOKEN = os.environ.get('VAGO_AGENT_TOKEN', '')
CALLBACK_TOKEN = os.environ.get('VAGO_CALLBACK_TOKEN', '')
DEFAULT_TENANT = os.environ.get('VAGO_TENANT_ID', '')

mcp = FastMCP(
    'vago-cloud',
    instructions=(
        'API de VAGO Cloud (sincronización de stock/precios/productos entre un '
        'Excel maestro y Shopify, MercadoLibre, Falabella, Ripley y Rappi). '
        'Multi-tenant: casi todas las operaciones requieren tenant_id (negocio). '
        'Usa list_tenants para descubrir los negocios y sus ids. Para endpoints '
        'sin tool dedicada usa vago_api_request.'
    ),
)


def _request(
    method: str,
    path: str,
    *,
    tenant_id: int | str | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 60.0,
    token: str | None = None,
) -> Any:
    auth_token = token if token is not None else TOKEN
    if not auth_token:
        raise RuntimeError(
            'Falta VAGO_AGENT_TOKEN. Crea un token en Gestión de cuenta → Agentes IA '
            'y expórtalo en el entorno del servidor MCP.'
        )
    if not path.startswith('/'):
        path = '/' + path
    headers = {'X-Agent-Token': auth_token}
    tenant = tenant_id if tenant_id not in (None, '') else DEFAULT_TENANT
    if tenant not in (None, ''):
        headers['X-Tenant-Id'] = str(tenant)

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        res = client.request(
            method.upper(), f'{API_URL}{path}', headers=headers,
            json=json_body, params=params,
        )
    body: Any
    try:
        body = res.json()
    except ValueError:
        body = res.text[:5000]
    if res.status_code == 401:
        raise RuntimeError(
            f'401 No autorizado: token inválido/revocado o switch de agentes en OFF. Detalle: {body}'
        )
    if res.status_code >= 400:
        raise RuntimeError(f'HTTP {res.status_code} en {method} {path}: {json.dumps(body, ensure_ascii=False)[:2000]}')
    return body


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ── Identidad y plataforma ────────────────────────────────────────────────────

@mcp.tool()
def whoami() -> str:
    """Usuario con el que actúa este agente (rol, tenant, features)."""
    return _dump(_request('GET', '/api/auth/me/'))


@mcp.tool()
def platform_overview() -> str:
    """Vista global de la plataforma (todos los negocios) — requiere superadmin."""
    return _dump(_request('GET', '/api/auth/platform-overview/'))


# ── Tenants (negocios) ────────────────────────────────────────────────────────

@mcp.tool()
def list_tenants() -> str:
    """Lista los negocios (tenants) con id, nombre, plan, estado y fuente."""
    return _dump(_request('GET', '/api/auth/tenants/'))


@mcp.tool()
def create_tenant(
    nombre: str,
    plan: str = 'trial',
    estado: str = 'trial',
    fuente_tipo: str = 'excel',
    notas: str = '',
) -> str:
    """Crea un negocio nuevo. fuente_tipo: 'excel' | 'shopify'."""
    return _dump(_request('POST', '/api/auth/tenants/', json_body={
        'nombre': nombre, 'plan': plan, 'estado': estado,
        'fuente_tipo': fuente_tipo, 'notas': notas,
    }))


@mcp.tool()
def update_tenant(tenant_pk: int, changes: dict) -> str:
    """PATCH parcial de un negocio. changes admite: nombre, plan, estado, fuente_tipo, notas, features."""
    return _dump(_request('PATCH', f'/api/auth/tenants/{tenant_pk}/', json_body=changes))


# ── Usuarios ──────────────────────────────────────────────────────────────────

@mcp.tool()
def list_users() -> str:
    """Lista los usuarios de la plataforma (requiere superadmin)."""
    return _dump(_request('GET', '/api/auth/users/'))


@mcp.tool()
def create_user(
    username: str,
    email: str,
    password: str,
    role: str = 'analista',
    tenant_id: int | None = None,
    first_name: str = '',
    last_name: str = '',
) -> str:
    """Crea un usuario. role: superadmin | staff | analista | vendedor."""
    body: dict = {
        'username': username, 'email': email, 'password': password,
        'role': role, 'first_name': first_name, 'last_name': last_name,
    }
    if tenant_id is not None:
        body['tenant_id'] = tenant_id
    return _dump(_request('POST', '/api/auth/users/', json_body=body))


@mcp.tool()
def update_user(user_pk: int, changes: dict) -> str:
    """PATCH parcial de un usuario. changes admite: email, role, tenant_id, is_active, password, first_name, last_name."""
    return _dump(_request('PATCH', f'/api/auth/users/{user_pk}/', json_body=changes))


# ── Operación diaria: jobs, stock, sync ──────────────────────────────────────

@mcp.tool()
def dashboard_summary(tenant_id: int) -> str:
    """Resumen operativo del negocio: métricas, estado de fuente, alertas."""
    return _dump(_request('GET', '/api/jobs/summary/', tenant_id=tenant_id))


@mcp.tool()
def list_jobs(tenant_id: int) -> str:
    """Últimos jobs de sincronización del negocio (estado, canal, tiempos)."""
    return _dump(_request('GET', '/api/jobs/', tenant_id=tenant_id))


@mcp.tool()
def get_job(job_id: int, tenant_id: int) -> str:
    """Detalle de un job, incluido su log."""
    return _dump(_request('GET', f'/api/jobs/{job_id}/', tenant_id=tenant_id))


@mcp.tool()
def retry_job(job_id: int, tenant_id: int) -> str:
    """Reintenta un job fallido o en dead-letter."""
    return _dump(_request('POST', f'/api/jobs/{job_id}/retry/', tenant_id=tenant_id))


@mcp.tool()
def trigger_sync(tenant_id: int, canal: str = 'all') -> str:
    """
    Encola sincronización de stock. canal: 'all' o uno de
    shopify | mercadolibre | falabella | ripley | mundotec.
    MUTACIÓN: actualiza stock real en las tiendas del negocio.
    """
    return _dump(_request(
        'POST', '/api/jobs/sync/', tenant_id=tenant_id,
        json_body={'canal': canal}, timeout=120.0,
    ))


@mcp.tool()
def stock_preview(tenant_id: int) -> str:
    """Preview del próximo sync (qué cambiaría por canal, sin tocar marketplaces)."""
    return _dump(_request('GET', '/api/jobs/preview/', tenant_id=tenant_id, timeout=90.0))


@mcp.tool()
def not_found_skus(tenant_id: int) -> str:
    """SKUs de la fuente que cada marketplace no reconoce (diagnóstico por canal)."""
    return _dump(_request('GET', '/api/jobs/not-found/', tenant_id=tenant_id, timeout=90.0))


@mcp.tool()
def maestro_status(tenant_id: int) -> str:
    """Estado del snapshot del archivo maestro en servidor (SKUs, antigüedad)."""
    return _dump(_request('GET', '/api/channels/maestro-status/', tenant_id=tenant_id))


@mcp.tool()
def refresh_maestro(tenant_id: int) -> str:
    """Relee el archivo maestro (OneDrive/Excel) y actualiza el snapshot en servidor."""
    return _dump(_request('POST', '/api/channels/maestro-refresh/', tenant_id=tenant_id, timeout=120.0))


@mcp.tool()
def list_channels(tenant_id: int) -> str:
    """Canales/tiendas del negocio: activo, configurado, último job (sin secretos)."""
    return _dump(_request('GET', '/api/channels/', tenant_id=tenant_id))


# ── Productos ─────────────────────────────────────────────────────────────────

@mcp.tool()
def create_products_shopify(tenant_id: int, skus: list[str], modo: str = 'ingresos') -> str:
    """
    Crea productos en Shopify desde el maestro. modo: 'ingresos' | 'preventa'.
    MUTACIÓN: publica productos reales.
    """
    return _dump(_request(
        'POST', '/api/channels/product-create-shopify/', tenant_id=tenant_id,
        json_body={'modo': modo, 'skus': skus}, timeout=120.0,
    ))


@mcp.tool()
def create_products_marketplace(tenant_id: int, canal: str, skus: list[str]) -> str:
    """
    Da de alta productos (ya existentes en Shopify) en un marketplace.
    canal: mercadolibre | falabella | ripley. MUTACIÓN.
    """
    return _dump(_request(
        'POST', '/api/channels/product-create-mktp/', tenant_id=tenant_id,
        json_body={'canal': canal, 'skus': skus}, timeout=120.0,
    ))


# ── Analytics ─────────────────────────────────────────────────────────────────

@mcp.tool()
def analytics_overview(tenant_id: int, month: str = '', lite: bool = True) -> str:
    """
    Analytics del negocio (ventas, metas, comparativos por canal).
    month: 'YYYY-MM' (vacío = mes actual). lite=True responde rápido desde cache.
    """
    params: dict = {}
    if month:
        params['month'] = month
    if lite:
        params['lite'] = '1'
    return _dump(_request(
        'GET', '/api/channels/analytics-overview/', tenant_id=tenant_id,
        params=params, timeout=120.0,
    ))


# ── AgentTask conversacional (Joji Dev Mode) ─────────────────────────────────

@mcp.tool()
def agent_task_update(
    task_id: int,
    status: str,
    agent_notes: str = '',
    agent_messages: list[dict] | None = None,
) -> str:
    """
    Callback para reportar estado/mensajes de una AgentTask a VAGO Cloud.
    Usa el callback token compartido (no el token de agente de usuario).
    status: investigating | awaiting_confirmation | executing | in_progress | pr_ready | done | failed.
    agent_messages: lista de {'role': 'agent', 'content': str, 'stage': str}.
    """
    if not CALLBACK_TOKEN:
        raise RuntimeError('Falta VAGO_CALLBACK_TOKEN para callbacks de AgentTask.')
    body: dict = {'status': status}
    if agent_notes:
        body['agent_notes'] = agent_notes
    if agent_messages:
        body['agent_messages'] = agent_messages
    return _dump(_request(
        'POST', f'/api/agents/tasks/{task_id}/agent-update/',
        json_body=body, token=CALLBACK_TOKEN, timeout=60.0,
    ))


@mcp.tool()
def confirm_agent_task(task_id: int, confirmation_token: str) -> str:
    """
    Confirma una AgentTask en awaiting_confirmation para que VAGO re-despache
    stage: execute. Requiere el confirmation_token devuelto por agent_task_update.
    """
    if not CALLBACK_TOKEN:
        raise RuntimeError('Falta VAGO_CALLBACK_TOKEN para callbacks de AgentTask.')
    return _dump(_request(
        'POST', f'/api/agents/tasks/{task_id}/confirm/',
        json_body={'confirmation_token': confirmation_token},
        token=CALLBACK_TOKEN, timeout=60.0,
    ))


# ── Escape hatch: cualquier endpoint ─────────────────────────────────────────

@mcp.tool()
def vago_api_request(
    method: str,
    path: str,
    tenant_id: int | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
) -> str:
    """
    Request genérico a la API de VAGO Cloud para endpoints sin tool dedicada.
    method: GET|POST|PATCH|PUT|DELETE. path: ej. '/api/channels/pricing-compare/'.
    El token actúa como el usuario emisor; mismo poder que la app. Las rutas de
    administración de agentes (/api/agents/*) rechazan tokens de agente por diseño.
    """
    return _dump(_request(
        method, path, tenant_id=tenant_id, json_body=json_body,
        params=params, timeout=120.0,
    ))


# ── Shopify Admin multi-tienda (selección por parámetro `store`) ─────────────
# Tools: shopify_list_stores, shopify_admin_query, shopify_admin_mutation,
# shopify_update_product_seo. Credenciales por tienda vía ChannelConfig (con
# respaldo por entorno). server.py usa FastMCP, así que el registro es directo.
from vago_shopify import register_shopify_tools  # noqa: E402

register_shopify_tools(mcp)


def _run_http() -> None:
    """
    Sirve el MCP por HTTP (streamable-http) para uso remoto (p.ej. co-ubicado con
    el backend de prod, conectado desde Claude desktop vía `mcp-remote`).

    Protección: header `Authorization: Bearer <VAGO_MCP_SHARED_SECRET>`. Si el
    secreto no está seteado, el server NO arranca (fail-safe: este endpoint puede
    leer/escribir Shopify).
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    secret = os.environ.get('VAGO_MCP_SHARED_SECRET', '').strip()
    if not secret:
        raise SystemExit(
            'VAGO_MCP_SHARED_SECRET vacío. Define un secreto para exponer el MCP por '
            'HTTP (el endpoint puede mutar Shopify). Aborta por seguridad.'
        )

    host = os.environ.get('VAGO_MCP_HOST', '0.0.0.0')
    port = int(os.environ.get('VAGO_MCP_PORT', '9000'))
    mcp.settings.host = host
    mcp.settings.port = port
    # Stateless: sin afinidad de sesión, más robusto detrás de un load balancer.
    mcp.settings.stateless_http = True

    # FastMCP activa DNS-rebinding protection en localhost por defecto; detrás de
    # Traefik el Host real es mcp.vagocloud.com y el SDK respondía 421 tras pasar
    # Bearer auth. Whitelist configurable (coma-separada) + variantes con puerto.
    from mcp.server.transport_security import TransportSecuritySettings

    raw_hosts = os.getenv(
        'VAGO_MCP_ALLOWED_HOSTS',
        'mcp.vagocloud.com,localhost,127.0.0.1',
    ).split(',')
    allowed_hosts: list[str] = []
    for entry in raw_hosts:
        h = entry.strip()
        if not h:
            continue
        allowed_hosts.append(h)
        if not h.endswith(':*') and ']:' not in h:
            allowed_hosts.append(f'{h}:*')
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
    )

    mcp_path = mcp.settings.streamable_http_path  # default '/mcp'
    app = mcp.streamable_http_app()

    class _BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Solo protege las rutas del MCP; deja pasar health checks u otras.
            if request.url.path.startswith(mcp_path):
                if request.headers.get('authorization', '') != f'Bearer {secret}':
                    return JSONResponse({'error': 'unauthorized'}, status_code=401)
            return await call_next(request)

    app.add_middleware(_BearerAuth)
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get('VAGO_MCP_LOG_LEVEL', 'info'))


if __name__ == '__main__':
    # VAGO_MCP_TRANSPORT: 'stdio' (default, uso local Claude Code/Cursor) | 'http'
    # (servicio remoto en prod, autenticado por secreto).
    transport = os.environ.get('VAGO_MCP_TRANSPORT', 'stdio').strip().lower()
    if transport in ('http', 'streamable-http', 'streamable_http'):
        _run_http()
    else:
        mcp.run()
