import os
import io
import json
import time
import hmac
import hashlib
import secrets
import tempfile
import stripe
import resend

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from markitdown import MarkItDown
from collections import defaultdict
import threading

# ── RATE LIMITING ──────────────────────────────────────────────────────────────
_rate_lock    = threading.Lock()
_ip_attempts: dict = defaultdict(list)
RATE_MAX      = 5    # intentos por ventana
RATE_WINDOW   = 600  # segundos (10 min)

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        _ip_attempts[ip] = [t for t in _ip_attempts[ip] if now - t < RATE_WINDOW]
        if len(_ip_attempts[ip]) >= RATE_MAX:
            return False
        _ip_attempts[ip].append(now)
        return True

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY         = os.environ.get("RESEND_API_KEY", "")
PRO_SECRET             = os.environ.get("PRO_SECRET", "tokenslayer-super-secret-2025-jota")
EMAIL_FROM             = os.environ.get("EMAIL_FROM", "TokenSlayer <noreply@jsantos.xyz>")
SITE_URL               = os.environ.get("SITE_URL", "https://tokenslayer.netlify.app")
SUPPORT_EMAIL          = "support@iapacks.com"

# IDs de productos/precios del Box Kit en Stripe (pago único)
# Stripe identifica los pagos por el price_id o por el payment_link
BOX_BASIC_LINK   = os.environ.get("BOX_BASIC_LINK",   "bJe8wOa3Uehn5p85LO2Ry0d")   # $9
BOX_PRO_LINK     = os.environ.get("BOX_PRO_LINK",     "eVq28qcc28X34l4cac2Ry0e")   # $67
BOX_AGENCY_LINK  = os.environ.get("BOX_AGENCY_LINK",  "5kQ00i8ZQ4GN6tc0ru2Ry0j")   # $297
BOX_SETUP_LINK   = os.environ.get("BOX_SETUP_LINK",   "cNi14mdg61uBdVE1vy2Ry0h")   # $697
BOX_SITE_URL    = os.environ.get("BOX_SITE_URL",   "https://apps.iapacks.com")

stripe.api_key = STRIPE_SECRET_KEY
resend.api_key = RESEND_API_KEY

# ─────────────────────────────────────────────
#  SESIONES EN MEMORIA (24h)
#  Render free resetea el proceso ocasionalmente
#  — el usuario simplemente vuelve a hacer login
# ─────────────────────────────────────────────
sessions: dict[str, dict] = {}   # { session_token: { email, expires_at } }

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

FREE_EXTENSIONS  = {".pdf", ".docx", ".doc", ".txt", ".csv", ".html", ".htm"}
PRO_EXTENSIONS   = {".pptx", ".ppt", ".xlsx", ".xls", ".xml", ".json"}
ALL_EXTENSIONS   = FREE_EXTENSIONS | PRO_EXTENSIONS

# ─────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────
app = FastAPI(title="TokenSlayer API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(email: str) -> str:
    token = generate_session_token()
    sessions[token] = {
        "email": email,
        "expires_at": time.time() + 86400  # 24 horas
    }
    # Limpieza oportunista de sesiones caducadas
    expired = [k for k, v in sessions.items() if v["expires_at"] < time.time()]
    for k in expired:
        del sessions[k]
    return token


def validate_session(session_token: str | None) -> str | None:
    """Devuelve el email si la sesión es válida, None si no."""
    if not session_token:
        return None
    session = sessions.get(session_token)
    if not session:
        return None
    if session["expires_at"] < time.time():
        del sessions[session_token]
        return None
    return session["email"]


def email_has_active_stripe_subscription(email: str) -> bool:
    """Busca en Stripe si el email tiene suscripción activa (TokenSlayer PRO)."""
    try:
        customers = stripe.Customer.list(email=email, limit=10)
        for customer in customers.auto_paging_iter():
            subs = stripe.Subscription.list(customer=customer.id, status="active", limit=5)
            if subs.data:
                return True
        return False
    except Exception:
        return False


# Emails de administrador — acceso directo sin verificar Stripe
ADMIN_EMAILS = {
    "jsantospro3@gmail.com":    "setup",
    "support@iapacks.com":      "setup",
    "desireedanchau@gmail.com": "agency",  # test temporal - borrar después
}

def email_has_box_purchase(email: str) -> dict | None:
    """
    Busca en Stripe si el email tiene una compra del Box Kit.
    Busca tanto en Customers como en checkout sessions de invitados.
    Devuelve dict con 'plan' ('basic'|'pro'|'agency'|'setup') o None.
    """
    def identify_plan(pl: str, amount: int, session_id: str = "") -> str | None:
        pl = str(pl)
        # 1. Por payment_link — más fiable
        if BOX_SETUP_LINK  in pl: return "setup"
        if BOX_AGENCY_LINK in pl: return "agency"
        if BOX_PRO_LINK    in pl: return "pro"
        if BOX_BASIC_LINK  in pl: return "basic"
        # 2. Por amount real (sin cupón)
        if amount >= 69700: return "setup"
        if amount >= 29700: return "agency"
        if amount >= 6700:  return "pro"
        if amount >= 900:   return "basic"
        # 3. Por nombre del producto en line_items (funciona con cualquier cupón)
        if session_id:
            try:
                items = stripe.checkout.Session.list_line_items(session_id, limit=1)
                if items and items.data:
                    desc = str(items.data[0].get("description", "") or "").lower()
                    if "setup" in desc: return "setup"
                    if "agency" in desc: return "agency"
                    if "pro" in desc and "box" in desc: return "pro"
                    if "básico" in desc or "basico" in desc or "basic" in desc: return "basic"
                    # Si tiene cualquier línea del Box Kit, es al menos basic
                    if "business box" in desc or "iapacks" in desc: return "basic"
            except Exception as ex:
                print(f"Error line_items {session_id}: {ex}")
        return None

    try:
        # 1. Buscar por Customer (compradores registrados)
        customers = stripe.Customer.list(email=email, limit=10)
        for customer in customers.auto_paging_iter():
            sessions = stripe.checkout.Session.list(
                customer=customer.id,
                status="complete",
                limit=20
            )
            for session in sessions.auto_paging_iter():
                pl     = str(session.get("payment_link", "") or "")
                amount = session.get("amount_total", 0) or 0
                plan   = identify_plan(pl, amount, session.get("id",""))
                if plan:
                    return {"plan": plan, "email": email}

        # 2. Buscar en checkout sessions por email (invitados / guest checkout)
        # Iterar todas las sessions completadas buscando el email
        starting_after = None
        found = False
        while not found:
            kwargs = {"status": "complete", "limit": 100}
            if starting_after:
                kwargs["starting_after"] = starting_after
            batch = stripe.checkout.Session.list(**kwargs)
            if not batch.data:
                break
            for session in batch.data:
                cd = session.get("customer_details") or {}
                customer_email = cd.get("email", "") or ""
                print(f"  Checking session {session.get('id','')} → {customer_email}")
                if customer_email.lower() == email.lower():
                    pl     = str(session.get("payment_link", "") or "")
                    amount = session.get("amount_total", 0) or 0
                    sid    = session.get("id", "")
                    plan   = identify_plan(pl, amount, sid)
                    if plan:
                        print(f"✓ Guest encontrado: {email} → {plan}")
                        return {"plan": plan, "email": email}
            if batch.has_more:
                starting_after = batch.data[-1].id
            else:
                break

        return None
    except Exception as e:
        print(f"Error buscando compra Box Kit: {e}")
        return None


def send_welcome_email(email: str, customer_name: str = "") -> bool:
    """Envía email de bienvenida PRO tras el pago."""
    try:
        name_display = customer_name if customer_name else "crack"
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [email],
            "reply_to": SUPPORT_EMAIL,
            "subject": "✓ Bienvenido a TokenSlayer PRO — ya tienes acceso",
            "html": f"""
<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:40px 20px">

    <div style="text-align:center;margin-bottom:40px">
      <span style="font-size:32px;font-weight:900;color:#fff">Token<span style="color:#a855f7">Slayer</span></span>
      <p style="color:#6b7280;margin:8px 0 0">Convierte. Ahorra. Úsalo bien.</p>
    </div>

    <div style="background:#111;border:1px solid #222;border-radius:16px;padding:32px">
      <div style="text-align:center;margin-bottom:24px">
        <div style="display:inline-block;background:#a855f715;border:1px solid #a855f740;border-radius:50px;padding:8px 20px">
          <span style="color:#a855f7;font-weight:700">★ PRO ACTIVO</span>
        </div>
      </div>

      <h1 style="color:#fff;font-size:24px;font-weight:700;margin:0 0 12px">
        Hola{', ' + customer_name if customer_name else ''} — ya eres PRO.
      </h1>

      <p style="color:#9ca3af;line-height:1.6;margin:0 0 24px">
        Tu acceso está listo. Sin tokens que copiar, sin contraseñas que recordar.
        Entra con tu email y listo.
      </p>

      <div style="background:#0a0a0a;border:1px solid #333;border-radius:12px;padding:20px;margin-bottom:24px">
        <p style="color:#6b7280;font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:1px">Cómo acceder</p>
        <ol style="color:#e5e7eb;margin:0;padding-left:20px;line-height:2">
          <li>Ve a <a href="{SITE_URL}" style="color:#a855f7">{SITE_URL}</a></li>
          <li>Pulsa el botón <strong style="color:#fff">★ PRO</strong></li>
          <li>Escribe este email: <strong style="color:#a855f7">{email}</strong></li>
          <li>Pulsa <strong style="color:#fff">Acceder</strong> — listo</li>
        </ol>
      </div>

      <div style="background:#a855f710;border:1px solid #a855f730;border-radius:12px;padding:20px;margin-bottom:24px">
        <p style="color:#a855f7;font-weight:700;margin:0 0 8px">Lo que tienes con PRO</p>
        <ul style="color:#9ca3af;margin:0;padding-left:20px;line-height:2">
          <li>PowerPoint, Excel, XML, JSON — todos los formatos</li>
          <li>Archivos hasta <strong style="color:#e5e7eb">50 MB</strong></li>
          <li>MarkItDown real en servidor</li>
          <li>Historial de conversiones</li>
        </ul>
      </div>

      <p style="color:#6b7280;font-size:14px;margin:0 0 8px">
        ¿Algún problema? Responde a este email — te llega directo a soporte.
      </p>
    </div>

    <div style="text-align:center;margin-top:32px;padding-top:24px;border-top:1px solid #1f1f1f">
      <p style="color:#4b5563;font-size:13px;margin:0 0 12px">
        Mientras tienes TokenSlayer PRO, hay otra herramienta que te interesa:
      </p>
      <a href="https://apps.iapacks.com" style="display:block;margin-bottom:16px">
        <img src="https://Aka625.b-cdn.net/tokens%20slayers/AI%20BUsiness%20box%20(4).png"
             alt="AI Business Box Pro — Plug &amp; Sell"
             style="width:100%;max-width:480px;border-radius:12px;display:block;margin:0 auto"/>
      </a>
      <a href="https://apps.iapacks.com" style="display:inline-block;background:#a855f7;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:700">
        AI Business Box Kit — $9 básico · $67 Pro →
      </a>
      <p style="color:#4b5563;font-size:12px;margin:12px 0 0">
        9 apps listas para usar o vender. Plug &amp; Sell.
      </p>
    </div>

    <p style="text-align:center;color:#374151;font-size:12px;margin-top:24px">
      by <strong>Jota!</strong> · iapacks.com · TokenSlayer<br>
      Soporte: <a href="mailto:{SUPPORT_EMAIL}" style="color:#6b7280">{SUPPORT_EMAIL}</a>
    </p>
  </div>
</body>
</html>
""",
        })
        return True
    except Exception as e:
        print(f"Error enviando email: {e}")
        return False


# ─────────────────────────────────────────────
#  ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
def send_box_welcome_email(email: str, plan: str, customer_name: str = "") -> bool:
    """Envía email de bienvenida del AI Business Box Kit según el plan."""
    try:
        plans = {
            "basic":  {"label": "Básico",        "color": "#2060d0", "url": f"{BOX_SITE_URL}/acceso/",        "emoji": "📦"},
            "pro":    {"label": "Pro",            "color": "#00c896", "url": f"{BOX_SITE_URL}/acceso-pro/",    "emoji": "⚡"},
            "agency": {"label": "Agency",         "color": "#f0a020", "url": f"{BOX_SITE_URL}/acceso-agency/", "emoji": "🚀"},
            "setup":  {"label": "Agency + Setup", "color": "#f0a020", "url": f"{BOX_SITE_URL}/acceso-agency/", "emoji": "🚀"},
        }
        p      = plans.get(plan, plans["basic"])
        label  = p["label"]
        color  = p["color"]
        url    = p["url"]
        emoji  = p["emoji"]
        name   = customer_name if customer_name else ""
        hola   = f"Hola{', ' + name if name else ''} — tu panel está listo."
        badge  = f"{emoji} PLAN {label.upper()} ACTIVO"

        html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#07080f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:40px 20px">
    <div style="text-align:center;margin-bottom:36px">
      <img src="https://Aka625.b-cdn.net/logos%20plugin/Screenshot_3.jpg" alt="AI Business Box Kit" style="height:56px;border-radius:8px"/>
    </div>
    <div style="background:#0b0d1a;border:1px solid rgba(240,160,32,0.25);border-radius:16px;padding:32px;position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent,{color},transparent)"></div>
      <div style="text-align:center;margin-bottom:24px">
        <div style="display:inline-block;background:{color}20;border:1px solid {color}50;border-radius:50px;padding:8px 24px">
          <span style="color:{color};font-weight:800;font-size:14px">{badge}</span>
        </div>
      </div>
      <h1 style="color:#eeeef5;font-size:24px;font-weight:800;margin:0 0 12px;text-align:center">{hola}</h1>
      <p style="color:#9ca3af;line-height:1.7;margin:0 0 28px;text-align:center">
        Tienes acceso inmediato al <strong style="color:#eeeef5">AI Business Box Kit · Plan {label}</strong>. Sin contraseñas. Solo tu email.
      </p>
      <div style="background:#07080f;border:1px solid #1f2030;border-radius:12px;padding:20px;margin-bottom:24px">
        <p style="color:#6b7280;font-size:12px;margin:0 0 12px;text-transform:uppercase;letter-spacing:1px">Cómo acceder</p>
        <ol style="color:#e5e7eb;margin:0;padding-left:20px;line-height:2.2;font-size:14px">
          <li>Ve a <a href="{url}" style="color:{color};font-weight:700">{url}</a></li>
          <li>Introduce este email: <strong style="color:{color}">{email}</strong></li>
          <li>Clic en Acceder — listo</li>
        </ol>
      </div>
      <div style="text-align:center;margin-bottom:24px">
        <a href="{url}" style="display:inline-block;padding:14px 36px;border-radius:50px;background:{color};color:#07080f;font-weight:800;font-size:14px;text-decoration:none">
          {emoji} Acceder a mi panel →
        </a>
      </div>
      <p style="color:#4b5563;font-size:13px;margin:0;text-align:center">
        ¿Problemas? Responde a este email — soporte directo.<br>
        <span style="color:{color}">support@iapacks.com</span>
      </p>
    </div>
    <div style="text-align:center;margin-top:28px">
      <p style="color:#374151;font-size:12px;margin:0">AI Business Box Kit · Plug &amp; Sell · by Jota! · iapacks.com</p>
    </div>
  </div>
</body>
</html>"""

        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [email],
            "reply_to": SUPPORT_EMAIL,
            "subject": f"⚡ Tu acceso al AI Business Box Kit · Plan {label} — ya está listo",
            "html": html
        })
        return True
    except Exception as e:
        print(f"Error email Box Kit: {e}")
        return False


def root():
    return {"status": "ok", "service": "TokenSlayer API", "version": "2.0.0"}


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "healthy", "timestamp": time.time()}


# ── LOGIN por email contra Stripe ─────────────
class LoginRequest(BaseModel):
    email: str


@app.post("/login")
async def login(request: Request, body: LoginRequest):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera 10 minutos.")
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email inválido.")

    has_sub = email_has_active_stripe_subscription(email)
    if not has_sub:
        raise HTTPException(
            status_code=403,
            detail="No encontramos suscripción PRO activa para este email. ¿Usaste otro email al pagar?"
        )

    session_token = create_session(email)
    return {"session_token": session_token, "email": email, "expires_in": 86400}


# ── VERIFICAR sesión activa ───────────────────
@app.get("/session")
async def check_session(x_session_token: str | None = Header(default=None)):
    email = validate_session(x_session_token)
    if not email:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
    return {"valid": True, "email": email}


# ── CONVERTIR archivo ────────────────────────
@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    x_session_token: str | None = Header(default=None)
):
    # Validar tamaño
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande. Máximo 50 MB.")

    filename  = file.filename or "archivo"
    extension = os.path.splitext(filename)[1].lower()

    is_pro_user  = validate_session(x_session_token) is not None
    is_pro_format = extension in PRO_EXTENSIONS

    # Formato PRO sin sesión PRO
    if is_pro_format and not is_pro_user:
        raise HTTPException(
            status_code=403,
            detail=f"El formato {extension.upper()} es exclusivo PRO. Accede con tu email PRO o suscríbete en {SITE_URL}#precios"
        )

    # Formato no soportado
    if extension not in ALL_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Formato no soportado: {extension}. Formatos Free: PDF, Word, TXT, CSV, HTML. Formatos PRO: PPTX, Excel, XML, JSON."
        )

    # Convertir con MarkItDown
    try:
        with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        md = MarkItDown()
        result = md.convert(tmp_path)
        markdown_text = result.text_content

        os.unlink(tmp_path)

        # Calcular ahorro de tokens (estimación estándar: 1 token ≈ 4 chars)
        original_tokens  = len(contents) // 4
        markdown_tokens  = len(markdown_text) // 4
        saved_tokens     = max(0, original_tokens - markdown_tokens)
        reduction_pct    = round((saved_tokens / original_tokens * 100) if original_tokens > 0 else 0, 1)

        return {
            "markdown": markdown_text,
            "filename": os.path.splitext(filename)[0] + ".md",
            "stats": {
                "original_tokens":  original_tokens,
                "markdown_tokens":  markdown_tokens,
                "saved_tokens":     saved_tokens,
                "reduction_pct":    reduction_pct,
                "original_size_kb": round(len(contents) / 1024, 1),
            },
            "pro": is_pro_user,
        }

    except Exception as e:
        # Limpiar si quedó el tmp
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error al convertir el archivo: {str(e)}")


# ── LOGIN BOX KIT (pago único) ───────────────
@app.post("/box/login")
async def box_login(request: Request, body: LoginRequest):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera 10 minutos.")
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email inválido.")

    # Admin bypass — acceso directo sin verificar Stripe
    if email in ADMIN_EMAILS:
        purchase = {"plan": ADMIN_EMAILS[email], "email": email}
    else:
        purchase = email_has_box_purchase(email)
    if not purchase:
        raise HTTPException(
            status_code=403,
            detail="No encontramos ninguna compra del AI Business Box Kit para este email. ¿Usaste otro email al pagar?"
        )

    session_token = create_session(email)
    # Guardamos el plan en la sesión
    sessions[session_token]["plan"] = purchase["plan"]

    plan = purchase["plan"]
    # agency y setup tienen el mismo acceso
    access_plan = "agency" if plan in ("agency", "setup") else plan

    return {
        "session_token": session_token,
        "email": email,
        "plan": access_plan,
        "original_plan": plan,
        "expires_in": 86400
    }


# ── VERIFICAR sesión Box Kit ──────────────────
@app.get("/box/session")
async def box_check_session(x_session_token: str | None = Header(default=None)):
    email = validate_session(x_session_token)
    if not email:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
    plan = sessions.get(x_session_token, {}).get("plan", "basic")
    return {"valid": True, "email": email, "plan": plan}


# ── STRIPE WEBHOOK ───────────────────────────
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma Stripe inválida.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Nuevo cliente suscrito
    if event["type"] == "customer.subscription.created":
        subscription = event["data"]["object"]
        customer_id  = subscription.get("customer")

        try:
            customer = stripe.Customer.retrieve(customer_id)
            email    = customer.get("email", "")
            raw_name = customer.get("name", "") or ""
            name_parts = raw_name.strip().split()
            name     = name_parts[0].capitalize() if name_parts else ""

            if email:
                send_welcome_email(email, name)
                print(f"✓ Email bienvenida enviado a {email}")
        except Exception as e:
            print(f"Error procesando webhook: {e}")

    # Pago puntual — distinguir TokenSlayer vs Box Kit
    elif event["type"] == "checkout.session.completed":
        session      = event["data"]["object"]
        email        = session.get("customer_details", {}).get("email", "")
        raw_name     = session.get("customer_details", {}).get("name", "") or ""
        # Limpiar nombre — coger solo el primer token si parece basura
        name_parts   = raw_name.strip().split()
        name         = name_parts[0].capitalize() if name_parts else ""
        # payment_link puede ser string ID o dict con 'id'
        pl_raw = session.get("payment_link", "") or ""
        if isinstance(pl_raw, dict):
            payment_link = str(pl_raw.get("id", ""))
        else:
            payment_link = str(pl_raw)

        # También buscar en metadata y en el amount como fallback
        amount = session.get("amount_total", 0) or 0

        # Intentar obtener el nombre del producto desde line_items
        product_name = ""
        try:
            session_id = session.get("id", "")
            if session_id:
                line_items = stripe.checkout.Session.list_line_items(session_id, limit=1)
                if line_items and line_items.data:
                    product_name = str(line_items.data[0].get("description", "") or "").lower()
        except Exception:
            pass

        if email:
            # Identificar por payment_link primero
            if BOX_SETUP_LINK in payment_link:
                send_box_welcome_email(email, "setup", name)
                print(f"✓ Email Box Kit Setup enviado a {email}")
            elif BOX_AGENCY_LINK in payment_link:
                send_box_welcome_email(email, "agency", name)
                print(f"✓ Email Box Kit Agency enviado a {email}")
            elif BOX_PRO_LINK in payment_link:
                send_box_welcome_email(email, "pro", name)
                print(f"✓ Email Box Kit Pro enviado a {email}")
            elif BOX_BASIC_LINK in payment_link:
                send_box_welcome_email(email, "basic", name)
                print(f"✓ Email Box Kit Básico enviado a {email}")
            elif amount >= 69700:
                send_box_welcome_email(email, "setup", name)
                print(f"✓ Email Box Kit Setup (por amount) enviado a {email}")
            elif amount >= 29700:
                send_box_welcome_email(email, "agency", name)
                print(f"✓ Email Box Kit Agency (por amount) enviado a {email}")
            elif amount >= 6700:
                send_box_welcome_email(email, "pro", name)
                print(f"✓ Email Box Kit Pro (por amount) enviado a {email}")
            elif amount >= 900:
                send_box_welcome_email(email, "basic", name)
                print(f"✓ Email Box Kit Básico (por amount) enviado a {email}")
            elif "agency" in product_name or "setup" in product_name:
                send_box_welcome_email(email, "agency", name)
                print(f"✓ Email Box Kit Agency (por nombre) enviado a {email}")
            elif "pro" in product_name and "box" in product_name:
                send_box_welcome_email(email, "pro", name)
                print(f"✓ Email Box Kit Pro (por nombre) enviado a {email}")
            elif "básico" in product_name or "basico" in product_name:
                send_box_welcome_email(email, "basic", name)
                print(f"✓ Email Box Kit Básico (por nombre) enviado a {email}")
            else:
                # TokenSlayer u otro producto
                send_welcome_email(email, name)
                print(f"✓ Email TokenSlayer enviado a {email}")

    return {"received": True}
