from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

    # Produk Healthify yang sudah ada — TIDAK diubah.
    path('api/', include('api.urls')),

    # Kapabilitas tambahan: Health Intelligence Engine (dipakai HealthTalk).
    path('api/v1/intelligence/', include('api.intelligence_urls')),

    # Dokumentasi API eksternal (Scalar).
    path('', include('api.docs_urls')),
]
