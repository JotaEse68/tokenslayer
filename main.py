from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import tempfile, os, hashlib, hmac, time, secrets

app = FastAPI(title="TokenSlayer API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tokenslayer.netlify.app",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Variables de entorno ────────────────────────────────────────────────────
PRO_SECRET            = os.environ.get("PRO_SECRET", "cambia-esto")
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY        = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM            = os.environ.get("EMAIL_FROM", "TokenSlayer <noreply@jsantos.xyz>")
EMAIL_SUPPORT         = "support@iapacks.com"
SITE_URL              = os.environ.get("SITE_URL", "https://tokenslayer.netlify.app")
MAX_FILE_SIZE         = 50 * 1024 * 1024  # 50 MB
# ───────────────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {
    "pdf","docx","doc","pptx","ppt",
    "xlsx","xls","csv","txt","md",
    "html","htm","xml","json"
}

# Sesiones activas en memoria: {session_token: {email, expires_at}}
# En producción esto iría en Redis o base de datos
active_sessions = {}

SESSION_DURATION = 24 * 60 * 60  # 24 horas en segundos


# ── Utilidades ──────────────────────────────────────────────────────────────

def generate_session_token(email: str) -> str:
    """Genera un token de sesión único."""
    return secrets.token_urlsafe(32)


def is_session_valid(session_token: str) -> str | None:
    """Verifica si una sesión es válida. Devuelve el email o None."""
    session = active_sessions.get(session_token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        del active_sessions[session_token]
        return None
    return session["email"]


async def check_stripe_subscription(email: str) -> bool:
    """Consulta Stripe para verificar si el email tiene suscripción activa."""
    if not STRIPE_SECRET_KEY:
        return False
    import httpx
    async with httpx.AsyncClient() as client:
        # Buscar clientes por email
        resp = await client.get(
            "https://api.stripe.com/v1/customers",
            params={"email": email, "limit": 5},
            auth=(STRIPE_SECRET_KEY, "")
        )
        if resp.status_code != 200:
            return False
        customers = resp.json().get("data", [])
        if not customers:
            return False

        # Verificar suscripciones activas de cada cliente
        for customer in customers:
            sub_resp = await client.get(
                "https://api.stripe.com/v1/subscriptions",
                params={
                    "customer": customer["id"],
                    "status": "active",
                    "limit": 5
                },
                auth=(STRIPE_SECRET_KEY, "")
            )
            if sub_resp.status_code == 200:
                subs = sub_resp.json().get("data", [])
                # Verificar que alguna suscripción sea de TokenSlayer
                for sub in subs:
                    for item in sub.get("items", {}).get("data", []):
                        product_id = item.get("price", {}).get("product", "")
                        # Cualquier suscripción activa en esta cuenta de Stripe = PRO
                        if product_id:
                            return True
    return False


async def send_pro_email(email: str, session_token: str):
    """Envía email de bienvenida PRO con link de acceso directo."""
    if not RESEND_API_KEY:
        print(f"[EMAIL SKIP] Sesión para {email}: {session_token}")
        return

    access_url = f"{SITE_URL}?session={session_token}"

    body_html = f"""
    <div style="font-family:monospace;background:#141614;color:#e8e6df;padding:32px;border-radius:12px;max-width:480px">
      <h2 style="color:#00e676;font-size:24px;margin-bottom:8px">Token<span style="font-style:italic">Slayer</span> PRO</h2>
      <p style="color:#a8a89e;margin-bottom:24px">Tu acceso PRO está activo.</p>

      <p style="color:#a8a89e;margin-bottom:8px;font-size:12px;letter-spacing:0.1em;text-transform:uppercase">Cómo acceder</p>
      <ol style="color:#a8a89e;font-size:13px;line-height:1.8;padding-left:20px;margin-bottom:24px">
        <li>Ve a <a href="{SITE_URL}" style="color:#00c853">{SITE_URL}</a></li>
        <li>Pulsa <strong style="color:#e8e6df">★ PRO</strong></li>
        <li>Escribe tu email: <strong style="color:#00e676">{email}</strong></li>
        <li>Pulsa <strong style="color:#e8e6df">Acceder</strong> — listo</li>
      </ol>

      <p style="color:#a8a89e;font-size:13px;margin-bottom:8px">Tu acceso se renueva automáticamente mientras tu suscripción esté activa. Sin tokens, sin copiar nada — solo tu email.</p>

      <p style="color:#4a4d48;font-size:11px;border-top:1px solid #4a4d48;padding-top:16px;margin-top:16px">
        ¿Problemas? Escribe a <a href="mailto:{EMAIL_SUPPORT}" style="color:#00c853">{EMAIL_SUPPORT}</a><br><br>
        — Jota. El que te ahorró los tokens.
      </p>
    </div>
    """

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": EMAIL_FROM,
                "reply_to": EMAIL_SUPPORT,
                "to": [email],
                "subject": "Tu acceso PRO de TokenSlayer",
                "html": body_html
            }
        )
        if resp.status_code != 200:
            print(f"[EMAIL ERROR] {resp.status_code}: {resp.text}")
        else:
            print(f"[EMAIL OK] Enviado a {email}")


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "TokenSlayer API", "version": "2.0.0"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/login")
async def login(request: Request):
    """
    El usuario introduce su email.
    Verificamos contra Stripe si tiene suscripción activa.
    Si sí, creamos una sesión de 24h.
    """
    body = await request.json()
    email = body.get("email", "").strip().lower()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email inválido.")

    print(f"[LOGIN] Verificando suscripción para {email}")

    has_subscription = await check_stripe_subscription(email)

    if not has_subscription:
        raise HTTPException(
            status_code=403,
            detail="No encontramos una suscripción PRO activa para este email. ¿Usaste otro email al pagar?"
        )

    # Crear sesión de 24 horas
    session_token = generate_session_token(email)
    active_sessions[session_token] = {
        "email": email,
        "expires_at": time.time() + SESSION_DURATION
    }

    print(f"[LOGIN OK] Sesión creada para {email}")
    return JSONResponse({
        "success": True,
        "session_token": session_token,
        "email": email,
        "expires_in": SESSION_DURATION
    })


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Webhook de Stripe — dispara email de bienvenida tras el pago."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print(f"[WEBHOOK ERROR] Firma inválida: {e}")
        raise HTTPException(status_code=400, detail="Firma inválida")

    event_type = event.get("type", "")
    print(f"[WEBHOOK] Evento recibido: {event_type}")

    if event_type in ("checkout.session.completed", "customer.subscription.created"):
        data = event["data"]["object"]
        email = (
            data.get("customer_email")
            or data.get("customer_details", {}).get("email")
            or ""
        )

        if email:
            # Crear sesión inicial y mandar email
            session_token = generate_session_token(email)
            active_sessions[session_token] = {
                "email": email,
                "expires_at": time.time() + SESSION_DURATION
            }
            print(f"[WEBHOOK] Sesión creada para {email}")
            await send_pro_email(email, session_token)
        else:
            print(f"[WEBHOOK WARN] No se encontró email en {event_type}")

    return JSONResponse({"received": True})


@app.post("/convert")
async def convert_file(
    file: UploadFile = File(...),
    x_session_token: str = Header(None, alias="X-Session-Token")
):
    """Convierte un archivo a Markdown. Requiere sesión PRO activa."""
    email = is_session_valid(x_session_token or "")
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Sesión inválida o expirada. Vuelve a introducir tu email."
        )

    ext = file.filename.split(".")[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Formato no soportado: .{ext}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande. Máximo 50 MB.")

    try:
        from markitdown import MarkItDown
        mid = MarkItDown()
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        result = mid.convert(tmp_path)
        os.unlink(tmp_path)
        markdown = result.text_content
        print(f"[CONVERT OK] {file.filename} → {len(markdown)} chars para {email}")
        return JSONResponse({
            "success": True,
            "filename": file.filename,
            "markdown": markdown,
            "stats": {
                "original_kb": round(len(content) / 1024, 1),
                "markdown_chars": len(markdown),
                "token_estimate": len(markdown) // 4,
            }
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en conversión: {str(e)}")
