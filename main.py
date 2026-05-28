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

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from markitdown import MarkItDown

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
    """Busca en Stripe si el email tiene suscripción activa."""
    try:
        customers = stripe.Customer.list(email=email, limit=10)
        for customer in customers.auto_paging_iter():
            subs = stripe.Subscription.list(customer=customer.id, status="active", limit=5)
            if subs.data:
                return True
        return False
    except Exception:
        return False


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
def root():
    return {"status": "ok", "service": "TokenSlayer API", "version": "2.0.0"}


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": time.time()}


# ── LOGIN por email contra Stripe ─────────────
class LoginRequest(BaseModel):
    email: str


@app.post("/login")
async def login(body: LoginRequest):
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
            name     = customer.get("name", "")

            if email:
                send_welcome_email(email, name)
                print(f"✓ Email bienvenida enviado a {email}")
        except Exception as e:
            print(f"Error procesando webhook: {e}")

    # Pago puntual (por si usas payment_intent en lugar de subscription)
    elif event["type"] == "checkout.session.completed":
        session     = event["data"]["object"]
        email       = session.get("customer_details", {}).get("email", "")
        name        = session.get("customer_details", {}).get("name", "")
        customer_id = session.get("customer")

        if email:
            send_welcome_email(email, name)
            print(f"✓ Email bienvenida (checkout) enviado a {email}")

    return {"received": True}
