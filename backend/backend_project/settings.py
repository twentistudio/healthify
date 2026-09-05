"""
Django settings for backend_project project.
"""

from pathlib import Path
from dotenv import load_dotenv
import os
from datetime import timedelta
import dj_database_url


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Try multiple possible training directory locations
_possible_training_dirs = [
    BASE_DIR / 'training',
    BASE_DIR.parent / 'training',
]
TRAINING_DIR = next((d for d in _possible_training_dirs if d.exists()), BASE_DIR / 'training')

ENV_PATH = TRAINING_DIR / '.env'
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    # Railway: env vars are injected directly, no .env file needed
    load_dotenv()  # fallback: load from process env or .env in cwd

# Root .env (kredensial aplikasi) — dimuat setelah training/.env.
# `override=False` (default) menjaga variabel yang sudah ada tetap menang.
_ROOT_ENV_PATH = BASE_DIR.parent / '.env'
if _ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=_ROOT_ENV_PATH)

# Normalisasi nama key Gemini.
# training/.env memakai `GEMINI_API`, sedangkan kode aplikasi membaca
# `GEMINI_API_KEY`. Tanpa penyelarasan ini fitur Gemini (terjemahan &
# embedding) diam-diam mati dan sistem jatuh ke provider lain.
if not os.getenv('GEMINI_API_KEY'):
    _gemini_alias = os.getenv('GEMINI_API') or os.getenv('GOOGLE_API_KEY')
    if _gemini_alias:
        os.environ['GEMINI_API_KEY'] = _gemini_alias

# Email Configuration (di bagian bawah file, sebelum LOGGING)
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

# Admin notification settings
ADMIN_NOTIFICATION_EMAILS = os.getenv('ADMIN_NOTIFICATION_EMAILS', '').split(',')
ADMIN_NOTIFICATION_EMAILS = [email.strip() for email in ADMIN_NOTIFICATION_EMAILS if email.strip()]

# Notification settings
ENABLE_EMAIL_NOTIFICATIONS = os.getenv('ENABLE_EMAIL_NOTIFICATIONS', 'True') == 'True'
NOTIFICATION_FROM_NAME = os.getenv('NOTIFICATION_FROM_NAME', 'Healthify System')

# For development - use console email backend
if os.getenv('DEBUG', 'True') == 'True':
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    

# SECURITY WARNING
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')

# Allow build phase (collectstatic) to run without SECRET_KEY
_is_collecting_static = 'collectstatic' in ' '.join(os.sys.argv)
if not SECRET_KEY and not _is_collecting_static:
    raise ValueError("The DJANGO_SECRET_KEY environment variable is not set.")
if not SECRET_KEY:
    SECRET_KEY = 'temporary-key-for-collectstatic-only'

# SECURITY WARNING
DEBUG = os.getenv('DEBUG', 'False') == 'True'

# TLS diterminasi di reverse proxy. Tanpa ini `request.is_secure()` selalu False
# dan URL absolut yang dibentuk Django memakai skema http — memicu blokir
# Mixed Content di halaman yang dilayani lewat https.
# Aman karena nginx di depan SELALU menulis ulang header ini (lihat nginx.conf).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# X-Forwarded-Host TIDAK dipercaya secara default: nginx sudah meneruskan
# header Host yang benar, sehingga mempercayainya hanya menambah permukaan
# host-header injection tanpa manfaat.
USE_X_FORWARDED_HOST = os.getenv('USE_X_FORWARDED_HOST', 'False') == 'True'

# Host yang dilayani sepenuhnya ditentukan environment — tidak ada domain
# yang di-hardcode. Wildcard seperti ".railway.app" sengaja TIDAK dipakai:
# itu menerima Host header dari subdomain mana pun milik penyedia tersebut.
#
# `localhost`/`127.0.0.1` tetap ada karena dipakai health check container.
ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# Hosts provided via environment (comma-separated)
env_allowed_hosts = os.getenv('ALLOWED_HOSTS', '')
if env_allowed_hosts:
    ALLOWED_HOSTS += [h.strip() for h in env_allowed_hosts.split(',') if h.strip()]

# Railway menyuntikkan RAILWAY_PUBLIC_DOMAIN (tanpa skema) — host persis, bukan wildcard.
railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
if railway_domain:
    ALLOWED_HOSTS.append(railway_domain)

ALLOWED_HOSTS = list(dict.fromkeys(h for h in ALLOWED_HOSTS if h))

# Trust env hosts for CSRF (HTTPS terminated at the reverse proxy)
CSRF_TRUSTED_ORIGINS = [
    f'https://{h}' for h in ALLOWED_HOSTS
    if h not in ('localhost', '127.0.0.1') and not h.startswith('.')
]

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third party apps
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    
    # Local apps
    'api',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend_project.wsgi.application'

# Database Configuration
# Priority: DATABASE_URL > Individual DB_* vars > SQLite fallback
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()

# Validate DATABASE_URL is a real URL (not empty, not unresolved template like ${{...}})
_is_valid_db_url = DATABASE_URL and DATABASE_URL.startswith(('postgres', 'postgresql', 'mysql', 'sqlite'))

if _is_valid_db_url:
    # Railway/Heroku style: use DATABASE_URL
    DATABASES = {
        'default': dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    # Check for individual PostgreSQL environment variables
    _db_name = os.getenv('DB_NAME') or os.getenv('PGDATABASE')
    _db_user = os.getenv('DB_USER') or os.getenv('PGUSER')
    _db_password = os.getenv('DB_PASSWORD') or os.getenv('PGPASSWORD')
    _db_host = os.getenv('DB_HOST') or os.getenv('PGHOST')
    _db_port = os.getenv('DB_PORT') or os.getenv('PGPORT', '5432')
    
    if all([_db_name, _db_user, _db_password, _db_host]):
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': _db_name,
                'USER': _db_user,
                'PASSWORD': _db_password,
                'HOST': _db_host,
                'PORT': _db_port,
            }
        }
    else:
        # Fallback to SQLite for local development
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': BASE_DIR / 'db.sqlite3',
            }
        }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'id'
TIME_ZONE = 'Asia/Jakarta'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# MEDIA FILES
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# CORS
#
# Daftar origin sepenuhnya eksplisit dan berasal dari environment.
#
# Yang DIHAPUS dan alasannya:
#   * domain yang di-hardcode -> setiap deployment mengaturnya sendiri.
#   * CORS_ALLOWED_ORIGIN_REGEXES = [r'^https://.*\.vercel\.app$']
#     Pola ini menerima SETIAP subdomain vercel.app. Dikombinasikan dengan
#     CORS_ALLOW_CREDENTIALS = True, siapa pun dapat menerbitkan situs di
#     vercel.app lalu membaca respons API ini atas nama pengunjung yang login.
#     Wildcard origin tidak boleh dipakai bersama kredensial.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = []

# FRONTEND_URL menerima satu origin ATAU beberapa origin dipisah koma,
# sehingga origin consumer eksternal (mis. HealthTalk) tidak butuh variabel
# tersendiri. Satu nilai tunggal tetap bekerja seperti sebelumnya.
for _source in (os.getenv('FRONTEND_URL', ''), os.getenv('CORS_ALLOWED_ORIGINS', '')):
    CORS_ALLOWED_ORIGINS += [
        origin.strip().rstrip('/')
        for origin in _source.split(',')
        if origin.strip()
    ]

if railway_domain:
    CORS_ALLOWED_ORIGINS.append(f'https://{railway_domain}')

CORS_ALLOWED_ORIGINS = list(dict.fromkeys(o for o in CORS_ALLOWED_ORIGINS if o))

# Regex origin hanya aktif bila diisi eksplisit lewat environment, dan tidak
# pernah berbarengan dengan kredensial (lihat CORS_ALLOW_CREDENTIALS di bawah).
CORS_ALLOWED_ORIGIN_REGEXES = [
    pattern.strip()
    for pattern in os.getenv('CORS_ALLOWED_ORIGIN_REGEXES', '').split(',')
    if pattern.strip()
]

# Frontend Healthify mengirim token lewat header Authorization, bukan cookie,
# dan consumer eksternal memakai X-API-Key. Tidak ada yang memerlukan kredensial
# lintas origin, jadi dimatikan — sekaligus menutup kelas serangan di atas.
CORS_ALLOW_CREDENTIALS = os.getenv('CORS_ALLOW_CREDENTIALS', 'False') == 'True'

if CORS_ALLOW_CREDENTIALS and CORS_ALLOWED_ORIGIN_REGEXES:
    raise ValueError(
        "CORS_ALLOW_CREDENTIALS tidak boleh aktif bersamaan dengan "
        "CORS_ALLOWED_ORIGIN_REGEXES: pola origin dapat mencocokkan domain "
        "milik pihak lain."
    )

CORS_ALLOW_METHODS = [
    'GET',
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
    'OPTIONS'
]

CORS_EXPOSE_HEADERS = ['x-request-id', 'x-idempotent-replay', 'retry-after']

CORS_ALLOW_HEADERS = [
    'accept',
    'authorization',
    'accept-encoding',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]
# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',  # Default untuk public endpoints
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'EXCEPTION_HANDLER': 'rest_framework.views.exception_handler',

    # Batas laju untuk endpoint intelligence (per API key consumer).
    # Setiap permintaan memanggil LLM + embedding berbayar, jadi tanpa batas
    # satu consumer bisa menghabiskan kuota milik Healthify.
    'DEFAULT_THROTTLE_RATES': {
        'intelligence': os.getenv('INTELLIGENCE_RATE_LIMIT', '60/min'),
        # Formulir permintaan akses terbuka tanpa kunci, karena memang
        # ditujukan bagi yang belum punya. Batas per IP menjaganya dari
        # pengiriman massal.
        'access_request': os.getenv('ACCESS_REQUEST_RATE_LIMIT', '5/hour'),
    },
}

# JWT Settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=2),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
    'AUDIENCE': None,
    'ISSUER': None,
    
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    
    'JTI_CLAIM': 'jti',
}

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'api': {
            'handlers': ['console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
    },
}

# Create logs directory if it doesn't exist
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)


# ============================================================================
# Health Intelligence Engine (kapabilitas tambahan — §21, §22)
#
# Semua setting di bawah OPSIONAL dan punya default aman. Healthify tetap
# berjalan persis seperti sebelumnya bila tidak satu pun diisi.
# ============================================================================

# Verifikasi DOI/URL sebelum dipublikasikan sebagai sumber.
# Matikan HANYA untuk pengujian offline: mematikannya berarti link tidak dicek.
EVIDENCE_LINK_CHECK_ENABLED = os.getenv('EVIDENCE_LINK_CHECK_ENABLED', 'True') == 'True'

# Bobot skor kualitas evidence (§15). Kosong = pakai default modul.
_evidence_weights = os.getenv('EVIDENCE_SCORE_WEIGHTS', '')
EVIDENCE_SCORE_WEIGHTS = {}
if _evidence_weights:
    import json as _json
    try:
        EVIDENCE_SCORE_WEIGHTS = _json.loads(_evidence_weights)
    except ValueError:
        EVIDENCE_SCORE_WEIGHTS = {}

# API key untuk consumer eksternal (HealthTalk dsb).
# Format env: "key1:healthtalk,key2:partner-lain"
# Bila kosong, endpoint /api/v1/intelligence/* terbuka (mode pengembangan).
# Format: "key:consumer" atau "key:consumer:batas" (mis. "k1:healthtalk:300/min").
# Batas per key berguna karena satu consumer backend melayani banyak pengguna
# sekaligus, sementara consumer lain mungkin hanya butuh sedikit.
INTELLIGENCE_API_KEYS = {}
INTELLIGENCE_KEY_RATES = {}
_raw_api_keys = os.getenv('INTELLIGENCE_API_KEYS', '').strip()
if _raw_api_keys:
    for _pair in _raw_api_keys.split(','):
        _parts = [_p.strip() for _p in _pair.split(':')]
        if len(_parts) >= 2 and _parts[0] and _parts[1]:
            INTELLIGENCE_API_KEYS[_parts[0]] = _parts[1]
            if len(_parts) >= 3 and _parts[2]:
                INTELLIGENCE_KEY_RATES[_parts[0]] = _parts[2]

# Izinkan mematikan pemanggilan LLM (engine tetap jalan dengan mode ekstraktif).
INTELLIGENCE_LLM_ENABLED = os.getenv('INTELLIGENCE_LLM_ENABLED', '1')

# Provider & model LLM.
# Memakai variabel yang SUDAH menjadi konvensi repo ini (dibaca juga oleh
# training/scripts/prompt_and_verify.py) — bukan variabel baru.
#   LLM_PROVIDER : "openai" | "gemini" | "groq" | ... (dipisah koma, EKSKLUSIF).
#                  Kosong = pakai semua provider berkredensial dengan fallback.
#   LLM_MODEL    : nama model. Kosong = default engine (gpt-5.4-mini).
LLM_PROVIDER = os.getenv('LLM_PROVIDER', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')

# Embedding teks (retrieval semantik & embed jurnal admin).
# Set 0 untuk mematikan pemanggilan API embedding — retrieval tetap jalan
# memakai pencocokan leksikal bilingual.
EMBEDDINGS_ENABLED = os.getenv('EMBEDDINGS_ENABLED', '1')

# Model embedding OpenAI. `text-embedding-3-small` mendukung parameter
# `dimensions`, sehingga keluarannya bisa dipotong agar cocok dengan kolom
# vektor yang sudah ada tanpa migrasi tabel.
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')

# Dimensi vektor. Kosongkan agar dideteksi otomatis dari tabel embeddings
# (pipeline training memakai 768).
EMBEDDING_DIMENSIONS = os.getenv('EMBEDDING_DIMENSIONS', '')

# Cache: dipakai untuk hitungan rate limit, kunci idempotensi, hasil validasi
# DOI/URL, dan terjemahan.
#
# Default DATABASE, bukan LocMemCache. LocMemCache hidup di memori tiap proses,
# sedangkan produksi menjalankan beberapa worker gunicorn — akibatnya setiap
# worker punya hitungan rate limit sendiri (batas 60/menit efektif menjadi
# 60 x jumlah worker) dan kunci idempotensi tidak terlihat oleh worker lain.
# Cache berbasis database dibagi seluruh worker tanpa menambah infrastruktur.
#
# Tabelnya dibuat oleh `manage.py createcachetable` (dijalankan saat start).
CACHES = {
    'default': {
        'BACKEND': os.getenv(
            'CACHE_BACKEND', 'django.core.cache.backends.db.DatabaseCache'
        ),
        'LOCATION': os.getenv('CACHE_LOCATION', 'healthify_cache'),
        'TIMEOUT': 300,
        'OPTIONS': {'MAX_ENTRIES': 20000, 'CULL_FREQUENCY': 4},
    }
}

# Berapa lama respons disimpan untuk kunci idempotensi (detik).
# Ke mana calon consumer mengirim permintaan akses API. Ditampilkan di
# dokumentasi bila diisi; dibiarkan kosong berarti dokumentasi tidak menyebut
# alamat apa pun (lebih baik daripada alamat karangan).
API_CONTACT_EMAIL = os.getenv('API_CONTACT_EMAIL', '').strip()
API_CONTACT_URL = os.getenv('API_CONTACT_URL', '').strip()

IDEMPOTENCY_TTL = int(os.getenv('IDEMPOTENCY_TTL', str(60 * 60 * 24)))

# Header yang dipakai consumer eksternal.
for _header in ('x-api-key', 'x-consumer', 'x-request-id', 'x-idempotency-key'):
    if _header not in CORS_ALLOW_HEADERS:
        CORS_ALLOW_HEADERS.append(_header)
