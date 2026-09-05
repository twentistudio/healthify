"""
Dokumentasi API eksternal, dirender dengan Scalar.

    GET /docs            -> halaman referensi API interaktif (Scalar)
    GET /openapi.json    -> spesifikasi OpenAPI 3.1

Halaman ini bersifat baca-saja dan tidak menyentuh data apa pun.
"""

import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.clickjacking import xframe_options_exempt

from .openapi import build_openapi_spec

logger = logging.getLogger(__name__)

SCALAR_CDN = "https://cdn.jsdelivr.net/npm/@scalar/api-reference"


def _base_url(request) -> str:
    """
    URL absolut untuk server ini.

    Skema diambil dari `X-Forwarded-Proto` (ditulis nginx di depan) lebih dulu,
    karena TLS diterminasi di reverse proxy dan `request.scheme` di dalam
    container selalu http.
    """
    forwarded_proto = (request.META.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip()
    if forwarded_proto in ("http", "https"):
        scheme = forwarded_proto
    else:
        scheme = "https" if request.is_secure() else request.scheme
    return f"{scheme}://{request.get_host()}"


def openapi_schema(request):
    """Spesifikasi OpenAPI 3.1 dalam format JSON."""
    try:
        spec = build_openapi_spec(base_url=_base_url(request))
    except Exception as exc:
        logger.error("[DOCS] gagal membangun spesifikasi OpenAPI: %s", exc, exc_info=True)
        return JsonResponse({"error": "spec_error", "detail": str(exc)}, status=500)

    response = JsonResponse(spec, json_dumps_params={"ensure_ascii": False})
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=300"
    return response


@xframe_options_exempt
def api_reference(request):
    """Halaman dokumentasi API (Scalar API Reference)."""
    # Path RELATIF, bukan URL absolut: browser mewarisi skema dan host halaman.
    # Ini menutup Mixed Content secara struktural, tidak bergantung pada
    # header proxy yang benar.
    spec_url = "/openapi.json"

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ragai API Reference</title>
  <meta name="description" content="ragai Health Intelligence API: health answers grounded in peer reviewed journal literature, with the papers attached." />
  <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🩺</text></svg>" />
  <style>
    body {{ margin: 0; }}

    /* Tombol dan formulir permintaan akses. Ditulis langsung di halaman ini,
       bukan di dalam spesifikasi, karena OpenAPI mendeskripsikan API dan bukan
       antarmuka. Scalar merender isi halaman sendiri, jadi elemen ini dijaga
       tetap di atasnya dengan z-index. */
    .req-home {{
      position: fixed; left: 18px; bottom: 20px; z-index: 60;
      font: 600 20px/1 Georgia, "Times New Roman", serif; letter-spacing: -.02em;
      text-decoration: none; color: #14181a; background: rgba(255, 255, 255, .92);
      padding: 9px 14px; border-radius: 999px; border: 1px solid #e3e6e2;
    }}
    .req-home span {{ color: #10574a; }}
    .req-home:hover {{ border-color: #10574a; }}

    .req-open {{
      position: fixed; right: 20px; bottom: 20px; z-index: 60;
      padding: 12px 20px; border: 0; border-radius: 999px;
      background: #10574a; color: #fff; cursor: pointer;
      font: 600 14px/1.2 system-ui, -apple-system, "Segoe UI", sans-serif;
      box-shadow: 0 6px 20px rgba(16, 87, 74, .32);
    }}
    .req-open:hover {{ background: #0c463b; }}
    .req-open:focus-visible {{ outline: 3px solid #8fd3bb; outline-offset: 2px; }}

    .req-backdrop {{
      position: fixed; inset: 0; z-index: 70; display: none;
      align-items: center; justify-content: center;
      background: rgba(15, 12, 24, .55); padding: 20px;
    }}
    .req-backdrop[data-open="true"] {{ display: flex; }}

    .req-card {{
      width: 100%; max-width: 520px; max-height: 90vh; overflow-y: auto;
      background: #fff; color: #1f2937; border-radius: 14px; padding: 26px;
      font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
      box-shadow: 0 24px 60px rgba(0, 0, 0, .3);
    }}
    .req-card h2 {{ margin: 0 0 6px; font-size: 20px; color: #0c463b; }}
    .req-card p.req-lead {{ margin: 0 0 18px; color: #6b7280; }}
    .req-card label {{ display: block; margin: 14px 0 5px; font-weight: 600; }}
    .req-card .req-optional {{ font-weight: 400; color: #9ca3af; }}
    .req-card input, .req-card textarea {{
      width: 100%; box-sizing: border-box; padding: 10px 12px;
      border: 1px solid #d1d5db; border-radius: 8px; font: inherit; color: inherit;
    }}
    .req-card input:focus, .req-card textarea:focus {{
      outline: none; border-color: #10574a; box-shadow: 0 0 0 3px rgba(16, 87, 74, .18);
    }}
    .req-card textarea {{ min-height: 96px; resize: vertical; }}
    .req-actions {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 22px; }}
    .req-actions button {{
      padding: 10px 18px; border-radius: 8px; font: 600 14px/1.2 inherit; cursor: pointer;
    }}
    .req-cancel {{ background: #fff; color: #4b5563; border: 1px solid #d1d5db; }}
    .req-submit {{ background: #10574a; color: #fff; border: 0; }}
    .req-submit[disabled] {{ opacity: .6; cursor: progress; }}
    .req-note {{ margin-top: 16px; padding: 12px 14px; border-radius: 8px; display: none; }}
    .req-note[data-kind="ok"] {{ display: block; background: #ecfdf5; color: #065f46; }}
    .req-note[data-kind="error"] {{ display: block; background: #fef2f2; color: #991b1b; }}

    @media (prefers-color-scheme: dark) {{
      .req-card {{ background: #1f2937; color: #e5e7eb; }}
      .req-card h2 {{ color: #8fd3bb; }}
      .req-card p.req-lead {{ color: #9ca3af; }}
      .req-card input, .req-card textarea {{
        background: #111827; border-color: #374151; color: #e5e7eb;
      }}
      .req-cancel {{ background: #111827; color: #d1d5db; border-color: #374151; }}
      .req-note[data-kind="ok"] {{ background: #064e3b; color: #d1fae5; }}
      .req-note[data-kind="error"] {{ background: #7f1d1d; color: #fee2e2; }}
    }}
  </style>
</head>
<body>
  <div id="app"></div>
  <script id="api-reference" data-url="{spec_url}"></script>
  <script>
    var configuration = {{
      theme: 'default',
      layout: 'modern',
      darkMode: false,
      hideDownloadButton: false,
      searchHotKey: 'k',
      metaData: {{
        title: 'ragai API Reference',
        description: 'Health answers grounded in peer reviewed journal literature.'
      }},
      defaultHttpClient: {{ targetKey: 'shell', clientKey: 'curl' }}
    }};
    document.getElementById('api-reference').dataset.configuration = JSON.stringify(configuration);
  </script>
  <script src="{SCALAR_CDN}"></script>

  <a class="req-home" href="/">rag<span>ai</span></a>

  <button class="req-open" type="button" data-req-open>Request API access</button>

  <div class="req-backdrop" data-req-backdrop role="dialog" aria-modal="true"
       aria-labelledby="req-title">
    <div class="req-card">
      <h2 id="req-title">Request API access</h2>
      <p class="req-lead">Tell us how you plan to use the engine. We review each
        request and send the API key to the email address below.</p>

      <form data-req-form novalidate>
        <label for="req-name">Name</label>
        <input id="req-name" name="name" type="text" required maxlength="200" autocomplete="name" />

        <label for="req-email">Email</label>
        <input id="req-email" name="email" type="email" required maxlength="254" autocomplete="email" />

        <label for="req-org">Organization <span class="req-optional">(optional)</span></label>
        <input id="req-org" name="organization" type="text" maxlength="200" autocomplete="organization" />

        <label for="req-use">How will you use the API?</label>
        <textarea id="req-use" name="use_case" required maxlength="2000"
                  placeholder="Product, audience, and the kind of questions you expect to send."></textarea>

        <label for="req-vol">Expected volume <span class="req-optional">(optional)</span></label>
        <input id="req-vol" name="expected_volume" type="text" maxlength="120"
               placeholder="e.g. 2000 requests per day" />

        <div class="req-note" data-req-note></div>

        <div class="req-actions">
          <button type="button" class="req-cancel" data-req-close>Cancel</button>
          <button type="submit" class="req-submit">Send request</button>
        </div>
      </form>
    </div>
  </div>

  <script>
    (function () {{
      var backdrop = document.querySelector('[data-req-backdrop]');
      var form = document.querySelector('[data-req-form]');
      var note = document.querySelector('[data-req-note]');
      var submit = form.querySelector('.req-submit');

      function open() {{
        backdrop.dataset.open = 'true';
        var first = form.querySelector('input');
        if (first) {{ first.focus(); }}
      }}

      function close() {{
        backdrop.dataset.open = 'false';
      }}

      function say(kind, message) {{
        note.dataset.kind = kind;
        note.textContent = message;
      }}

      document.querySelector('[data-req-open]').addEventListener('click', open);
      document.querySelector('[data-req-close]').addEventListener('click', close);

      // Klik pada latar menutup, klik di dalam kartu tidak.
      backdrop.addEventListener('click', function (event) {{
        if (event.target === backdrop) {{ close(); }}
      }});
      document.addEventListener('keydown', function (event) {{
        if (event.key === 'Escape' && backdrop.dataset.open === 'true') {{ close(); }}
      }});

      form.addEventListener('submit', function (event) {{
        event.preventDefault();

        var payload = {{
          name: form.name.value.trim(),
          email: form.email.value.trim(),
          organization: form.organization.value.trim(),
          use_case: form.use_case.value.trim(),
          expected_volume: form.expected_volume.value.trim()
        }};

        if (!payload.name || !payload.email || !payload.use_case) {{
          say('error', 'Name, email, and intended use are required.');
          return;
        }}

        submit.disabled = true;
        say('ok', 'Sending...');

        // Path relatif: browser mewarisi skema dan host halaman, sehingga tidak
        // ada kemungkinan permintaan http dari halaman https.
        fetch('/api/v1/intelligence/access-request', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }}).then(function (response) {{
          return response.json().then(function (body) {{
            return {{ ok: response.ok, status: response.status, body: body }};
          }});
        }}).then(function (result) {{
          submit.disabled = false;
          if (result.ok) {{
            form.reset();
            say('ok', 'Request received. We will contact you by email.');
            return;
          }}
          if (result.status === 429) {{
            say('error', 'Too many requests from this address. Please try again later.');
            return;
          }}
          say('error', (result.body && result.body.detail) || 'Could not send the request.');
        }}).catch(function () {{
          submit.disabled = false;
          say('error', 'Network error. Please try again.');
        }});
      }});
    }})();
  </script>
</body>
</html>
"""
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "public, max-age=300"
    return response
