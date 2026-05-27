from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import tempfile, os, hashlib, hmac, json

app = FastAPI(title="TokenSlayer API", version="1.1.0")

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

# ── Variables de entorno (configura en Render) ──────────────────────────────
PRO_SECRET        = os.environ.get("PRO_SECRET", "cambia-esto-en-render")
STRIPE_SECRET     = os.environ.get("STRIPE_WEBHOOK_SECRET", "")  # whsec_...
RESEND_API_KEY    = os.environ.get("RESEND_API_KEY", "")          # re_...
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "TokenSlayer <noreply@tokenslayer.com>")
SITE_URL          = os.environ.get("SITE_URL", "https://tokenslayer.netlify.app")
# ───────────────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {
    "pdf","docx","doc","pptx","ppt",
    "xlsx","xls","csv","txt","md",
    "html","htm","xml","json"
}
MAX_FILE_SIZE = 20 * 1024 * 1024


# ── Utilidades ──────────────────────────────────────────────────────────────

def generate_pro_token(email: str) -> str:
    """Genera un token PRO determinista a partir del email."""
    sig = hmac.new(
        PRO_SECRET.encode(),
        email.lower().encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return f"{email}:{sig}"


def verify_pro_token(token: str) -> bool:
    """Verifica que el token PRO sea válido."""
    if not token:
        return False
    if ":" not in token:
        return token == PRO_SECRET  # token maestro de admin
    email, sig = token.rsplit(":", 1)
    expected = hmac.new(
        PRO_SECRET.encode(),
        email.lower().encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


async def send_pro_email(email: str, token: str):
    """Envía el email con el token PRO via Resend."""
    if not RESEND_API_KEY:
        print(f"[EMAIL SKIP] Token para {email}: {token}")
        return

    import httpx
    body_html = f"""
    <div style="font-family:monospace;background:#141614;color:#e8e6df;padding:32px;border-radius:12px;max-width:480px">
      <h2 style="color:#00e676;font-size:24px;margin-bottom:8px">Token<span style="font-style:italic">Slayer</span> PRO</h2>
      <p style="color:#a8a89e;margin-bottom:24px">Tu acceso PRO está activo.</p>

      <p style="color:#a8a89e;margin-bottom:8px;font-size:12px;letter-spacing:0.1em;text-transform:uppercase">Tu token PRO</p>
      <div style="background:#003d1a;border:1px solid #00512a;border-radius:8px;padding:14px;margin-bottom:24px;word-break:break-all;color:#00e676;font-size:13px">
        {token}
      </div>

      <p style="color:#a8a89e;font-size:13px;margin-bottom:8px"><strong style="color:#e8e6df">Cómo usarlo:</strong></p>
      <ol style="color:#a8a89e;font-size:13px;line-height:1.8;padding-left:20px;margin-bottom:24px">
        <li>Ve a <a href="{SITE_URL}" style="color:#00c853">{SITE_URL}</a></li>
        <li>Pulsa el botón <strong style="color:#e8e6df">★ PRO</strong></li>
        <li>Pega tu token y pulsa <strong style="color:#e8e6df">verificar</strong></li>
        <li>Sube cualquier archivo — PowerPoint, Excel, PDF, lo que sea</li>
      </ol>

      <p style="color:#4a4d48;font-size:11px;border-top:1px solid #4a4d48;padding-top:16px;margin-top:8px">
        Guarda este email. El token no caduca mientras tu suscripción esté activa.<br>
        Si tienes problemas: responde a este email.<br><br>
        — Jota. El que te ahorró los tokens.
      </p>
    </div>
    """

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": EMAIL_FROM,
                "to": [email],
                "subject": "Tu token PRO de TokenSlayer",
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
    return {"status": "ok", "service": "TokenSlayer API", "version": "1.1.0"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """
    Webhook de Stripe — se dispara cuando alguien completa un pago.
    Configura en Stripe Dashboard → Webhooks → Add endpoint:
    URL: https://TU-APP.onrender.com/stripe-webhook
    Eventos: checkout.session.completed, customer.subscription.created
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verificar firma de Stripe
    try:
        import stripe
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_SECRET
        )
    except Exception as e:
        print(f"[WEBHOOK ERROR] Firma inválida: {e}")
        raise HTTPException(status_code=400, detail="Firma inválida")

    event_type = event.get("type", "")
    print(f"[WEBHOOK] Evento recibido: {event_type}")

    # Pago completado (Checkout one-time o primera cuota de suscripción)
    if event_type in ("checkout.session.completed", "customer.subscription.created"):
        data = event["data"]["object"]

        # Obtener email del cliente
        email = (
            data.get("customer_email")
            or data.get("customer_details", {}).get("email")
            or ""
        )

        if email:
            token = generate_pro_token(email)
            print(f"[TOKEN] Generado para {email}: {token}")
            await send_pro_email(email, token)
        else:
            print(f"[WEBHOOK WARN] No se encontró email en el evento {event_type}")

    return JSONResponse({"received": True})


@app.post("/convert")
async def convert_file(
    file: UploadFile = File(...),
    x_pro_token: str = Header(None, alias="X-Pro-Token")
):
    """Convierte un archivo a Markdown usando MarkItDown."""
    if not verify_pro_token(x_pro_token or ""):
        raise HTTPException(status_code=401, detail="Token PRO inválido o ausente.")

    ext = file.filename.split(".")[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Formato no soportado: .{ext}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande. Máximo 20 MB.")

    try:
        from markitdown import MarkItDown
        mid = MarkItDown()
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        result = mid.convert(tmp_path)
        os.unlink(tmp_path)
        markdown = result.text_content
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
