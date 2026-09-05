"""Routing halaman publik: perkenalan engine dan dokumentasinya (Scalar)."""

from django.urls import path

from .docs_views import api_reference, openapi_schema
from .landing_views import landing

urlpatterns = [
    # Halaman muka engine publik. Di domain Healthify, nginx melayani SPA pada
    # path ini dan rute berikut tidak pernah tercapai.
    path('', landing, name='landing'),

    path('docs', api_reference, name='api-docs'),
    path('docs/', api_reference, name='api-docs-slash'),
    path('openapi.json', openapi_schema, name='openapi-schema'),

    # Alias di bawah prefix /api/ agar mudah ditemukan.
    path('api/docs', api_reference, name='api-docs-prefixed'),
    path('api/docs/', api_reference, name='api-docs-prefixed-slash'),
    path('api/openapi.json', openapi_schema, name='openapi-schema-prefixed'),
]
