# TokenSlayer

Conversor de documentos a Markdown para optimizar tokens con Claude.
**Free** · **PRO $3,50/mes o $19/año** · pagos automáticos con Stripe

---

## Archivos

```
index.html        → Frontend (Netlify)
main.py           → Backend FastAPI (Render.com)
requirements.txt  → Dependencias Python
README.md         → Este archivo
```

---

## Deploy completo — orden exacto

### PASO 1 — GitHub
1. Crea repo `tokenslayer` en github.com
2. Sube los 4 archivos

---

### PASO 2 — Backend en Render.com

1. render.com → New → Web Service
2. Conecta tu repo GitHub → selecciona `tokenslayer`
3. Configuración:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Variables de entorno (en Render → Environment):

| Variable | Valor |
|---|---|
| `PRO_SECRET` | Una clave larga aleatoria — guárdala bien |
| `STRIPE_SECRET_KEY` | sk_live_... (de Stripe Dashboard) |
| `STRIPE_WEBHOOK_SECRET` | whsec_... (se obtiene en el paso 4) |
| `RESEND_API_KEY` | re_... (se obtiene en el paso 5) |
| `EMAIL_FROM` | TokenSlayer <noreply@tudominio.com> |
| `SITE_URL` | https://tokenslayer.netlify.app |

5. Deploy → copia la URL: `https://tokenslayer-api.onrender.com`

---

### PASO 3 — Stripe: dos productos de suscripción

1. dashboard.stripe.com → **Product catalog → Add product**
2. **Producto 1:** TokenSlayer PRO Mensual · $3,50 · recurrente mensual
3. **Producto 2:** TokenSlayer PRO Anual · $19 · recurrente anual
4. Para cada uno: **Payment Links → Create link** → copia la URL

---

### PASO 4 — Stripe Webhook (el que automatiza todo)

1. Stripe Dashboard → **Developers → Webhooks → Add endpoint**
2. **Endpoint URL:** `https://tokenslayer-api.onrender.com/stripe-webhook`
3. **Eventos a escuchar:**
   - `checkout.session.completed`
   - `customer.subscription.created`
4. Copia el **Signing secret** (`whsec_...`) → pégalo en Render como `STRIPE_WEBHOOK_SECRET`

Desde este momento: alguien paga → Stripe llama al webhook → backend genera token → email automático.

---

### PASO 5 — Resend (emails automáticos gratis)

1. resend.com → Create account (gratis hasta 3.000 emails/mes)
2. **API Keys → Create API Key** → copia la clave (`re_...`) → pégala en Render como `RESEND_API_KEY`
3. **Domains → Add domain** → añade tu dominio y verifica DNS
   (si no tienes dominio propio, usa el dominio que te dé Resend por defecto)

---

### PASO 6 — Frontend en Netlify

1. netlify.com → **Add new site → Deploy manually**
2. Arrastra el archivo `index.html`
3. Site name: `tokenslayer`
4. Edita las 3 líneas del CONFIG en `index.html`:

```javascript
var CONFIG = {
  backendUrl:    'https://tokenslayer-api.onrender.com',
  stripeMonthly: 'https://buy.stripe.com/TU_LINK_MENSUAL',
  stripeAnnual:  'https://buy.stripe.com/TU_LINK_ANUAL'
};
```

5. Vuelve a subir el `index.html` actualizado a Netlify

---

## Flujo automático completo

```
Cliente paga en Stripe
       ↓
Stripe dispara webhook → /stripe-webhook en Render
       ↓
Backend genera token PRO (email + firma HMAC)
       ↓
Resend manda email con el token + instrucciones
       ↓
Cliente pega token en TokenSlayer → acceso PRO inmediato
```

**Sin intervención manual. Sin scripts. Sin copy-paste.**

---

## Afiliados — actualiza los links

En `index.html` busca la sección `<!-- ===== AFILIADOS =====` y sustituye
los `href` por tus links de afiliado reales:

- Claude Pro → programa de afiliados de Anthropic
- Notion AI → notion.so/affiliates
- Perplexity → perplexity.ai/pro (programa de referidos)

---

## Ebook $9 — activar cuando esté listo

En `index.html` busca `ebook-cta soon` y:
1. Cambia la clase `soon` por nada
2. Cambia el `href="#"` por tu link de Gumroad o Stripe
3. Cambia el texto del botón por `Comprar — $9`

---

## Notas de producción

- Render tier gratuito: duerme tras 15 min de inactividad (~30s para despertar)
- Para eliminar cold start: Render Starter $7/mes
- Resend gratuito: 3.000 emails/mes — suficiente para empezar
- El token PRO es determinista: el mismo email siempre genera el mismo token
  (útil si alguien pierde el email — lo regeneras en 2 segundos)
