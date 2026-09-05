"""
Routing untuk Health Intelligence Engine (§21).

Dipasang di prefix TERPISAH (`/api/v1/intelligence/`) agar tidak pernah
bertabrakan dengan routing Healthify yang sudah ada di `api/urls.py`.
"""

from django.urls import path

from .intelligence_views import (
    AccessRequestView,
    ConsultationSummaryView,
    ConversationSessionView,
    IntelligenceCapabilitiesView,
    IntelligenceQueryView,
)

urlpatterns = [
    path('access-request', AccessRequestView.as_view(), name='intelligence-access-request'),
    path('access-request/', AccessRequestView.as_view(),
         name='intelligence-access-request-slash'),

    path('query', IntelligenceQueryView.as_view(), name='intelligence-query'),
    path('query/', IntelligenceQueryView.as_view(), name='intelligence-query-slash'),

    path('summary', ConsultationSummaryView.as_view(), name='intelligence-summary'),
    path('summary/', ConsultationSummaryView.as_view(), name='intelligence-summary-slash'),

    path('sessions/<str:session_id>', ConversationSessionView.as_view(),
         name='intelligence-session'),
    path('sessions/<str:session_id>/', ConversationSessionView.as_view(),
         name='intelligence-session-slash'),

    path('capabilities', IntelligenceCapabilitiesView.as_view(),
         name='intelligence-capabilities'),
    path('capabilities/', IntelligenceCapabilitiesView.as_view(),
         name='intelligence-capabilities-slash'),
]
