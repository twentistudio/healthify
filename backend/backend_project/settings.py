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

ALLOWED_HOSTS = [
    "healthify.cloud",
    "www.healthify.cloud", 
    "api.healthify.cloud",
    "localhost",
    "127.0.0.1",
    ".railway.app",  # Railway auto-generated domains
]

# Railway injects RAILWAY_PUBLIC_DOMAIN (without scheme)
railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
if railway_domain:
    ALLOWED_HOSTS.append(railway_domain)

# Hosts provided via environment (comma-separated)
env_allowed_hosts = os.getenv('ALLOWED_HOSTS', '')
if env_allowed_hosts:
    ALLOWED_HOSTS += [h.strip() for h in env_allowed_hosts.split(',') if h.strip()]

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

# CORS Settings
CORS_ALLOWED_ORIGINS = [
    "https://healthify.cloud",
    "https://www.healthify.cloud",
]

frontend_url = os.getenv('FRONTEND_URL')
if frontend_url:
    CORS_ALLOWED_ORIGINS.append(frontend_url.rstrip('/'))

# Railway: auto-add CORS for railway.app domains
if railway_domain:
    CORS_ALLOWED_ORIGINS.append(f'https://{railway_domain}')

# Vercel: allow all vercel.app preview deployments
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://.*\.vercel\.app$',
]

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_METHODS = [
    'GET',
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
    'OPTIONS'
]

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
