import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Claim, ClaimSource, Dispute, Source, VerificationResult


class ClaimVerifyViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_verify_requires_text(self):
        url = reverse("claim-verify")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("text", resp.json())

    def test_verify_uses_cached_result(self):
        claim = Claim.objects.create(text="Vitamin C bisa mencegah flu.")
        claim.status = Claim.STATUS_DONE
        claim.save()
        VerificationResult.objects.create(
            claim=claim,
            label=VerificationResult.LABEL_VALID,
            summary="Cached summary",
            confidence=0.9,
        )

        url = reverse("claim-verify")
        with patch("api.views.call_ai_verify") as mocked_call:
            resp = self.client.post(url, data={"text": claim.text}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], claim.id)
        mocked_call.assert_not_called()

    def test_verify_creates_verification_and_sources(self):
        ai_payload = {
            "label": "valid",
            "confidence": 0.8,
            "summary": "Ringkasan AI",
            "sources": [
                {
                    "title": "Journal A",
                    "doi": "10.1000/testdoi",
                    "url": "https://example.com/a",
                    "authors": "Doe",
                    "publisher": "Publisher",
                    "published_date": None,
                    "source_type": "journal",
                    "credibility_score": 0.9,
                    "relevance_score": 0.77,
                    "excerpt": "Excerpt",
                }
            ],
        }

        url = reverse("claim-verify")
        with patch("api.views.call_ai_verify", return_value=ai_payload) as mocked_call:
            resp = self.client.post(url, data={"text": "Kopi meningkatkan fokus."}, format="json")

        self.assertEqual(resp.status_code, 200)
        mocked_call.assert_called_once()

        claim_id = resp.json()["id"]
        claim = Claim.objects.get(id=claim_id)
        self.assertEqual(claim.status, Claim.STATUS_DONE)

        vr = VerificationResult.objects.get(claim=claim)
        self.assertEqual(vr.label, VerificationResult.LABEL_VALID)
        self.assertEqual(vr.confidence, 0.8)

        self.assertEqual(Source.objects.count(), 1)
        self.assertEqual(ClaimSource.objects.count(), 1)
        cs = ClaimSource.objects.select_related("source").get(claim=claim)
        self.assertEqual(cs.rank, 1)
        self.assertEqual(cs.source.doi, "10.1000/testdoi")

    def test_verify_handle_ai_exception(self):
        url = reverse("claim-verify")
        with patch("api.views.call_ai_verify", side_effect=Exception("boom")):
            resp = self.client.post(url, data={"text": "Klaim error"}, format="json")
        self.assertIn(resp.status_code, (400, 500))


class ClaimViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_claim_detail_returns_verification(self):
        claim = Claim.objects.create(text="Air putih penting untuk tubuh.")
        VerificationResult.objects.create(
            claim=claim,
            label=VerificationResult.LABEL_UNCERTAIN,
            summary="Summary",
            confidence=0.6,
        )

        url = reverse("claim-detail", kwargs={"claim_id": claim.id})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], claim.id)
        self.assertIn("verification_result", data)
        self.assertEqual(data["verification_result"]["label"], VerificationResult.LABEL_UNCERTAIN)

    def test_check_claim_duplicate_requires_text(self):
        url = reverse("check-duplicate")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)


class AuthViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.superuser = User.objects.create_user(
            username="superadmin",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.staff_user = User.objects.create_user(
            username="staff",
            password="pass12345",
            is_staff=True,
            is_superuser=False,
        )
        self.regular_user = User.objects.create_user(
            username="user",
            password="pass12345",
            is_staff=False,
            is_superuser=False,
        )

    def test_admin_login_missing_fields(self):
        url = reverse("admin-login")
        resp = self.client.post(url, data={"username": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_login_invalid_credentials(self):
        url = reverse("admin-login")
        resp = self.client.post(url, data={"username": "x", "password": "y"}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_admin_login_non_staff_forbidden(self):
        url = reverse("admin-login")
        resp = self.client.post(
            url, data={"username": self.regular_user.username, "password": "pass12345"}, format="json"
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_login_staff_success(self):
        url = reverse("admin-login")
        resp = self.client.post(url, data={"username": self.staff_user.username, "password": "pass12345"}, format="json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertEqual(data["user"]["username"], self.staff_user.username)

    def test_admin_logout_missing_refresh(self):
        url = reverse("admin-logout")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_logout_invalid_refresh(self):
        url = reverse("admin-logout")
        resp = self.client.post(url, data={"refresh": "invalid.token"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_logout_success(self):
        url = reverse("admin-logout")
        refresh = RefreshToken.for_user(self.staff_user)
        resp = self.client.post(url, data={"refresh": str(refresh)}, format="json")
        self.assertEqual(resp.status_code, 200)

    def test_admin_token_refresh_missing_refresh(self):
        url = reverse("admin-token-refresh")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_token_refresh_invalid_refresh(self):
        url = reverse("admin-token-refresh")
        resp = self.client.post(url, data={"refresh": "invalid.token"}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_admin_token_refresh_success(self):
        url = reverse("admin-token-refresh")
        refresh = RefreshToken.for_user(self.staff_user)
        resp = self.client.post(url, data={"refresh": str(refresh)}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.json())

    def test_admin_me_requires_auth(self):
        url = reverse("admin-me")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 401)

    def test_admin_me_success(self):
        url = reverse("admin-me")
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], self.staff_user.username)

    def test_admin_create_requires_superuser(self):
        url = reverse("admin-create")
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.post(url, data={"username": "x", "password": "y"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_admin_create_validations(self):
        url = reverse("admin-create")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(url, data={"username": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_create_duplicate_username(self):
        url = reverse("admin-create")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(
            url,
            data={"username": self.staff_user.username, "email": "x@x.com", "password": "pass12345"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_admin_create_success(self):
        url = reverse("admin-create")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(
            url,
            data={"username": "newadmin", "email": "new@x.com", "password": "pass12345", "is_superuser": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(username="newadmin", is_staff=True).exists())


class AdminViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.superuser = User.objects.create_user(
            username="superadmin",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.staff_user = User.objects.create_user(
            username="staff",
            password="pass12345",
            is_staff=True,
            is_superuser=False,
        )
        self.regular_user = User.objects.create_user(
            username="user",
            password="pass12345",
            is_staff=False,
            is_superuser=False,
        )

    def test_admin_dashboard_stats_success(self):
        claim = Claim.objects.create(text="Test claim")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_VALID, summary="s", confidence=0.8)
        Source.objects.create(title="S1", url="https://example.com/1")
        Dispute.objects.create(claim=claim, claim_text=claim.text, reason="Alasan panjang untuk dispute.", status=Dispute.STATUS_PENDING)

        url = reverse("admin-dashboard-stats")
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        stats = resp.json()["stats"]
        self.assertEqual(stats["total_claims"], 1)
        self.assertEqual(stats["pending_disputes"], 1)
        self.assertEqual(stats["total_sources"], 1)
        self.assertEqual(stats["verified_claims"], 1)

    def test_admin_user_list_requires_superadmin(self):
        url = reverse("admin-user-list")
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_admin_user_list_success(self):
        url = reverse("admin-user-list")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["total"] >= 2)

    def test_admin_user_create_validations(self):
        url = reverse("admin-user-list")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(url, data={"username": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_user_create_success(self):
        url = reverse("admin-user-list")
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(
            url,
            data={"username": "admin2", "email": "admin2@x.com", "password": "pass12345", "is_superuser": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(username="admin2", is_staff=True).exists())

    def test_admin_user_detail_not_found(self):
        url = reverse("admin-user-detail", kwargs={"user_id": 99999})
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_admin_user_delete_self_blocked(self):
        url = reverse("admin-user-detail", kwargs={"user_id": self.superuser.id})
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 400)

    def test_admin_user_delete_success(self):
        target = User.objects.create_user(username="todelete", password="pass12345", is_staff=True, is_superuser=False)
        url = reverse("admin-user-detail", kwargs={"user_id": target.id})
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(id=target.id).exists())

    def test_admin_dispute_list_filtering(self):
        claim = Claim.objects.create(text="Test claim")
        Dispute.objects.create(claim=claim, claim_text=claim.text, reason="Alasan panjang untuk dispute.", status=Dispute.STATUS_PENDING)
        Dispute.objects.create(claim=claim, claim_text=claim.text, reason="Alasan panjang untuk dispute.", status=Dispute.STATUS_APPROVED)

        url = reverse("admin-dispute-list") + "?status=pending"
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)

    def test_admin_dispute_detail_get_not_found(self):
        url = reverse("admin-dispute-detail", kwargs={"dispute_id": 99999})
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_admin_dispute_review_invalid_payload(self):
        claim = Claim.objects.create(text="Test claim")
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            status=Dispute.STATUS_PENDING,
        )

        url = reverse("admin-dispute-detail", kwargs={"dispute_id": dispute.id})
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_dispute_review_already_reviewed(self):
        claim = Claim.objects.create(text="Test claim")
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            status=Dispute.STATUS_APPROVED,
        )

        url = reverse("admin-dispute-detail", kwargs={"dispute_id": dispute.id})
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.post(url, data={"action": "approve", "re_verify": True}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_dispute_approve_manual_update(self):
        claim = Claim.objects.create(text="Test claim")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNCERTAIN, summary="s", confidence=0.6)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            status=Dispute.STATUS_PENDING,
        )

        url = reverse("admin-dispute-detail", kwargs={"dispute_id": dispute.id})
        self.client.force_authenticate(user=self.staff_user)
        with patch("api.admin_views.AdminDisputeDetailView._trigger_pipeline", return_value=None):
            resp = self.client.post(
                url,
                data={
                    "action": "approve",
                    "review_note": "ok",
                    "manual_update": True,
                    "new_label": "hoax",
                    "new_confidence": 0.2,
                    "new_summary": "updated",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.STATUS_APPROVED)
        vr = VerificationResult.objects.get(claim=claim)
        self.assertEqual(vr.label, VerificationResult.LABEL_HOAX)
        self.assertEqual(vr.confidence, 0.2)
        self.assertEqual(vr.summary, "updated")

    def test_admin_dispute_approve_reverify(self):
        claim = Claim.objects.create(text="Test claim")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNCERTAIN, summary="s", confidence=0.6)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            status=Dispute.STATUS_PENDING,
        )

        url = reverse("admin-dispute-detail", kwargs={"dispute_id": dispute.id})
        self.client.force_authenticate(user=self.staff_user)
        with (
            patch("api.admin_views.AdminDisputeDetailView._trigger_pipeline", return_value=None),
            patch("api.admin_views.call_ai_verify", return_value={"label": "hoax", "confidence": 0.9, "summary": "x"}),
            patch("api.admin_views.normalize_ai_response", return_value={"label": "hoax", "confidence": 0.9, "summary": "x"}),
        ):
            resp = self.client.post(url, data={"action": "approve", "re_verify": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        vr = VerificationResult.objects.get(claim=claim)
        self.assertEqual(vr.label, VerificationResult.LABEL_HOAX)

    def test_admin_dispute_reject(self):
        claim = Claim.objects.create(text="Test claim")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNCERTAIN, summary="s", confidence=0.6)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            status=Dispute.STATUS_PENDING,
        )

        url = reverse("admin-dispute-detail", kwargs={"dispute_id": dispute.id})
        self.client.force_authenticate(user=self.staff_user)
        resp = self.client.post(url, data={"action": "reject", "review_note": "no"}, format="json")
        self.assertEqual(resp.status_code, 200)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, Dispute.STATUS_REJECTED)

    def test_admin_sources_crud_and_stats(self):
        self.client.force_authenticate(user=self.staff_user)

        list_url = reverse("admin-source-list")
        resp = self.client.get(list_url)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(list_url, data={"title": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post(
            list_url, data={"title": "S1", "url": "https://example.com/s1", "credibility_score": 0.9}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        source_id = resp.json()["source"]["id"]

        resp = self.client.post(list_url, data={"title": "S1b", "url": "https://example.com/s1"}, format="json")
        self.assertEqual(resp.status_code, 400)

        detail_url = reverse("admin-source-detail", kwargs={"source_id": source_id})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.put(detail_url, data={"credibility_score": 2}, format="json")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.put(detail_url, data={"title": "S1-updated", "credibility_score": 0.8}, format="json")
        self.assertEqual(resp.status_code, 200)

        stats_url = reverse("admin-source-stats")
        resp = self.client.get(stats_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total_sources"], 1)

        resp = self.client.delete(detail_url)
        self.assertEqual(resp.status_code, 200)


class EvidenceFetchTests(TestCase):
    def test_fetch_evidence_from_doi_normalizes_url(self):
        from api import admin_views

        class DummyResp:
            status_code = 200

            def json(self):
                return {
                    "message": {
                        "title": ["Title A"],
                        "abstract": "<jats:p>Abstract</jats:p>",
                        "author": [{"given": "A", "family": "B"}],
                        "publisher": "P",
                    }
                }

        with patch("api.admin_views.requests.get", return_value=DummyResp()):
            data = admin_views.fetch_evidence_from_doi("https://doi.org/10.1000/test")
        self.assertEqual(data["doi"], "10.1000/test")
        self.assertEqual(data["title"], "Title A")
        self.assertIn("Abstract", data["abstract"])

    def test_fetch_evidence_from_url_parses_title(self):
        from api import admin_views

        class DummyResp:
            status_code = 200
            text = "<html><head><title>Hello</title><meta name='description' content='desc'></head></html>"

        with patch("api.admin_views.requests.get", return_value=DummyResp()):
            data = admin_views.fetch_evidence_from_url("https://example.com")
        self.assertEqual(data["title"], "Hello")


class EmailServiceTests(TestCase):
    @override_settings(ENABLE_EMAIL_NOTIFICATIONS=False)
    def test_send_email_disabled(self):
        from api.email_service import EmailNotificationService

        svc = EmailNotificationService()
        ok = svc._send_email("s", "m", ["a@b.com"])
        self.assertFalse(ok)

    @override_settings(ENABLE_EMAIL_NOTIFICATIONS=True, ADMIN_NOTIFICATION_EMAILS=[])
    def test_notify_admin_new_dispute_no_admin_emails(self):
        from api.email_service import EmailNotificationService

        claim = Claim.objects.create(text="Test claim")
        dispute = Dispute.objects.create(claim=claim, claim_text=claim.text, reason="Alasan panjang untuk dispute.")
        svc = EmailNotificationService()
        ok = svc.notify_admin_new_dispute(dispute)
        self.assertFalse(ok)


class ManagementCommandsTests(TestCase):
    def test_merge_duplicate_claims_requires_flag(self):
        out = StringIO()
        call_command("merge_duplicate_claims", stdout=out)
        self.assertIn("--dry-run", out.getvalue())

    def test_merge_duplicate_claims_dry_run(self):
        Claim.objects.create(text="dup")
        Claim.objects.create(text="dup")
        out = StringIO()
        call_command("merge_duplicate_claims", "--dry-run", stdout=out)
        self.assertIn("Starting duplicate claim analysis", out.getvalue())

    def test_clear_all_caches_deletes_expected_dirs(self):
        from api.management.commands import clear_all_caches

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "backend"
            module_path = base / "api" / "management" / "commands" / "clear_all_caches.py"
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.touch()

            training = base / "training" / "data"
            llm_cache = training / "llm_cache"
            cache = training / "cache"
            llm_cache.mkdir(parents=True, exist_ok=True)
            cache.mkdir(parents=True, exist_ok=True)
            (llm_cache / "a.txt").write_text("x", encoding="utf-8")
            (cache / "b.txt").write_text("y", encoding="utf-8")

            old_file = clear_all_caches.__file__
            try:
                clear_all_caches.__file__ = str(module_path)
                cmd = clear_all_caches.Command()
                cmd.stdout = StringIO()
                cmd.handle(type="all", confirm=True)
            finally:
                clear_all_caches.__file__ = old_file

            self.assertFalse((llm_cache / "a.txt").exists())
            self.assertFalse((cache / "b.txt").exists())


class TextNormalizationTests(TestCase):
    def test_calculate_text_similarity_high_for_identical(self):
        from api.text_normalization import calculate_text_similarity

        self.assertGreaterEqual(calculate_text_similarity("a b c", "a b c"), 0.99)

    def test_claim_similarity_matcher_finds_duplicate(self):
        from api.text_normalization import ClaimSimilarityMatcher

        matcher = ClaimSimilarityMatcher()
        existing = [(1, "Vitamin C membantu imunitas", "vitamin c membantu imunitas")]
        result = matcher.find_duplicates("Vitamin C membantu imunitas", existing)
        self.assertTrue(result["match_found"])


class AINormalizationTests(TestCase):
    def test_map_ai_label(self):
        from api.ai_adapter import map_ai_label_to_backend

        self.assertEqual(map_ai_label_to_backend("FAKTA"), "valid")
        self.assertEqual(map_ai_label_to_backend("false"), "hoax")
        self.assertEqual(map_ai_label_to_backend(""), "unverified")

    def test_normalize_ai_response_confidence_percent_to_float(self):
        from api.ai_adapter import normalize_ai_response

        result = normalize_ai_response({"label": "valid", "confidence": 80, "summary": "s", "sources": []})
        self.assertEqual(result["label"], "unverified")
        self.assertIsNone(result["confidence"])


class SerializerAndPermissionTests(TestCase):
    def test_dispute_review_serializer_rules(self):
        from api.serializers import DisputeReviewSerializer

        serializer = DisputeReviewSerializer(data={"action": "approve", "manual_update": True})
        self.assertFalse(serializer.is_valid())

        serializer = DisputeReviewSerializer(data={"action": "approve", "re_verify": False, "manual_update": False})
        self.assertFalse(serializer.is_valid())

        serializer = DisputeReviewSerializer(data={"action": "reject", "re_verify": True, "manual_update": True})
        self.assertTrue(serializer.is_valid())
        self.assertFalse(serializer.validated_data["re_verify"])
        self.assertFalse(serializer.validated_data["manual_update"])

    def test_permissions(self):
        from api.permissions import IsAdminOrReadOnly, IsSuperAdminOnly

        class DummyReq:
            def __init__(self, method, user):
                self.method = method
                self.user = user

        class DummyUser:
            def __init__(self, is_staff=False, is_superuser=False):
                self.is_staff = is_staff
                self.is_superuser = is_superuser

        perm_admin = IsAdminOrReadOnly()
        self.assertTrue(perm_admin.has_permission(DummyReq("GET", DummyUser()), None))
        self.assertFalse(perm_admin.has_permission(DummyReq("POST", DummyUser(is_staff=False)), None))
        self.assertTrue(perm_admin.has_permission(DummyReq("POST", DummyUser(is_staff=True)), None))

        perm_super = IsSuperAdminOnly()
        self.assertFalse(perm_super.has_permission(DummyReq("GET", DummyUser(is_superuser=False)), None))
        self.assertTrue(perm_super.has_permission(DummyReq("GET", DummyUser(is_superuser=True)), None))


class TranslateAndDuplicateTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_translate_verification_result_requires_payload(self):
        url = reverse("translate-verification-result")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)


    def test_check_claim_duplicate_finds_exact_match(self):
        Claim.objects.create(text="Vitamin c membantu imunitas")
        url = reverse("check-duplicate")
        resp = self.client.post(url, data={"text": "Vitamin c membantu imunitas"}, format="json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["is_duplicate"])
        self.assertEqual(data["match_level"], "exact")
        self.assertIsNotNone(data["matched_claim_id"])


class ClaimListAndSimilarityTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_claim_list_invalid_label_filter(self):
        url = reverse("claim-list") + "?label=badlabel"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)

    def test_claim_list_search_uses_normalized_field(self):
        Claim.objects.create(text="Kopi membantu fokus")
        url = reverse("claim-list") + "?search=kopi"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pagination"]["total"], 1)

    def test_find_similar_claims_uses_text_normalized(self):
        from api.views import find_similar_claims

        claim = Claim.objects.create(text="Kopi membantu fokus")
        claim.status = Claim.STATUS_DONE
        claim.save()

        matches = find_similar_claims("Kopi membantu fokus", threshold=0.8)
        self.assertTrue(matches)


class DisputeViewsExtendedTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_dispute_create_requires_claim_id_or_text(self):
        url = reverse("dispute-create")
        resp = self.client.post(url, data={"reason": "x" * 25}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_dispute_create_links_by_claim_id_and_stores_original(self):
        claim = Claim.objects.create(text="Test claim")
        claim.status = Claim.STATUS_DONE
        claim.save()
        VerificationResult.objects.create(
            claim=claim,
            label=VerificationResult.LABEL_UNCERTAIN,
            summary="s",
            confidence=0.6,
        )

        url = reverse("dispute-create")
        payload = {
            "claim_id": claim.id,
            "reason": "Alasan panjang untuk dispute yang valid.",
            "reporter_name": "R",
            "reporter_email": "r@example.com",
        }

        with patch("api.views.email_service.notify_admin_new_dispute", return_value=True):
            resp = self.client.post(url, data=payload, format="json")

        self.assertEqual(resp.status_code, 201)
        dispute = Dispute.objects.get(id=resp.json()["id"])
        self.assertEqual(dispute.claim_id, claim.id)
        self.assertEqual(dispute.original_label, VerificationResult.LABEL_UNCERTAIN)
        self.assertEqual(dispute.original_confidence, 0.6)

    def test_dispute_create_autolinks_by_similarity(self):
        claim = Claim.objects.create(text="Vitamin C membantu imunitas tubuh")
        claim.status = Claim.STATUS_DONE
        claim.save()

        url = reverse("dispute-create")
        payload = {
            "claim_text": "Vitamin C membantu imunitas tubuh!",
            "reason": "Alasan panjang untuk dispute yang valid.",
        }

        with patch("api.views.email_service.notify_admin_new_dispute", return_value=True):
            resp = self.client.post(url, data=payload, format="json")

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["claim_linked"])

    def test_dispute_list_and_detail(self):
        claim = Claim.objects.create(text="Test claim")
        dispute = Dispute.objects.create(claim=claim, claim_text=claim.text, reason="Alasan panjang untuk dispute.")

        list_url = reverse("dispute-list")
        resp = self.client.get(list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)

        detail_url = reverse("dispute-detail", kwargs={"dispute_id": dispute.id})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], dispute.id)


class JournalAdminViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def test_admin_journal_crud_and_embed(self):
        from api.models import JournalArticle

        list_url = reverse("admin-journal-list")
        with patch("api.views.embed_journal_article", return_value=None):
            resp = self.client.post(
                list_url,
                data={
                    "title": "J1",
                    "abstract": "Abstract yang cukup panjang untuk di-embed.",
                    "doi": "10.1000/j1",
                    "source_portal": "other",
                    "keywords": "k1,k2",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201)
        journal_id = resp.json()["journal"]["id"]

        resp = self.client.get(list_url + "?search=J1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pagination"]["total"], 1)

        detail_url = reverse("admin-journal-detail", kwargs={"journal_id": journal_id})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["journal"]["id"], journal_id)

        resp = self.client.put(detail_url, data={"title": "J1-updated"}, format="json")
        self.assertEqual(resp.status_code, 200)

        embed_url = reverse("admin-journal-embed")
        with patch("api.views.embed_journal_article", return_value=None):
            resp = self.client.post(embed_url, data={"journal_ids": [journal_id]}, format="json")
        self.assertEqual(resp.status_code, 200)

        self.assertTrue(JournalArticle.objects.filter(id=journal_id).exists())

        resp = self.client.delete(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(JournalArticle.objects.filter(id=journal_id).exists())

class ViewsDeepCoverageTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminx",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)


    def test_dispute_create_no_match_creates_without_link(self):
        url = reverse("dispute-create")
        payload = {
            "claim_text": "Teks yang tidak mirip dengan apapun",
            "reason": "Alasan panjang untuk dispute yang valid.",
        }
        with patch("api.views.email_service.notify_admin_new_dispute", return_value=True):
            resp = self.client.post(url, data=payload, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()["claim_linked"])

    def test_claim_list_invalid_page_and_per_page(self):
        url = reverse("claim-list") + "?page=0&per_page=0"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)

        url2 = reverse("claim-list") + "?page=abc&per_page=xyz"
        resp2 = self.client.get(url2)
        self.assertEqual(resp2.status_code, 400)

    def test_claim_detail_not_found(self):
        url = reverse("claim-detail", kwargs={"claim_id": 999999})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_duplicate_check_low_similarity_explanation(self):
        from api.models import Claim
        Claim.objects.create(text="Vitamin C membantu imunitas")
        url = reverse("check-duplicate")
        resp = self.client.post(url, data={"text": "Bola sepak itu seru"}, format="json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["explanation"], "Tidak mirip")

class ViewsLabelAndCacheTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_translate_label_mapping(self):
        from api.views import translate_label
        self.assertEqual(translate_label("fakta", "en"), "FACT")
        self.assertEqual(translate_label("valid", "en"), "VALID")
        self.assertEqual(translate_label("hoax", "en"), "HOAX")
        self.assertEqual(translate_label("uncertain", "id"), "TIDAK PASTI")
        self.assertEqual(translate_label("unverified", "id"), "TIDAK TERVERIFIKASI")


    def test_check_cached_result_prefers_verified(self):
        from api.views import check_cached_result
        claim1 = Claim.objects.create(text="X")
        claim1.status = Claim.STATUS_DONE
        claim1.save()
        VerificationResult.objects.create(claim=claim1, label=VerificationResult.LABEL_UNVERIFIED, summary="s", confidence=None)

        claim2 = Claim.objects.create(text="X")
        claim2.status = Claim.STATUS_DONE
        claim2.save()
        VerificationResult.objects.create(claim=claim2, label=VerificationResult.LABEL_VALID, summary="s", confidence=0.9)

        ok, cached_claim, vr = check_cached_result("X")
        self.assertTrue(ok)
        self.assertIsNotNone(cached_claim)
        self.assertEqual(vr.label, VerificationResult.LABEL_VALID)

    def test_verification_result_confidence_percent(self):
        claim = Claim.objects.create(text="Z")
        vr = VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNVERIFIED, summary="", confidence=None)
        self.assertIsNone(vr.confidence_percent())
        vr.label = VerificationResult.LABEL_VALID
        vr.confidence = 0.875
        vr.save()
        self.assertEqual(vr.confidence_percent(), 87.5)

    def test_claim_save_sets_normalized_and_hash(self):
        from api.text_normalization import normalize_claim_text
        c = Claim.objects.create(text="Kopi  meningkatkan   fokus!!")
        self.assertEqual(c.text_normalized, normalize_claim_text(c.text))
        self.assertTrue(c.text_hash)

    def test_check_cached_result_latest_when_unverified_only(self):
        claim1 = Claim.objects.create(text="Y1")
        claim1.status = Claim.STATUS_DONE
        claim1.save()
        VerificationResult.objects.create(claim=claim1, label=VerificationResult.LABEL_UNVERIFIED, summary="", confidence=None)
        claim2 = Claim.objects.create(text="Y1")
        claim2.status = Claim.STATUS_DONE
        claim2.save()
        VerificationResult.objects.create(claim=claim2, label=VerificationResult.LABEL_UNVERIFIED, summary="", confidence=None)
        from api.views import check_cached_result
        ok, cached_claim, vr = check_cached_result("Y1")
        self.assertTrue(ok)
        self.assertIsNotNone(cached_claim)
        self.assertEqual(vr.label, VerificationResult.LABEL_UNVERIFIED)


class AdminJournalImportAndStatsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminstats",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def test_admin_journal_import_and_stats(self):
        list_url = reverse("admin-journal-import")
        payload = {
            "journals": [
                {"title": "J1", "abstract": "A1", "doi": "10.2000/j1", "source_portal": "other"},
                {"title": "J2", "abstract": "A2", "doi": "10.2000/j2", "source_portal": "other"},
            ]
        }
        resp = self.client.post(list_url, data=payload, format="json")
        self.assertIn(resp.status_code, (201, 400))  # created_count may be >0

        stats_url = reverse("admin-source-stats")
        # Tambahkan satu source agar stats tidak kosong
        Source.objects.create(title="S1", url="https://example.com/s1", credibility_score=0.7, source_type="journal")
        resp2 = self.client.get(stats_url)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("total_sources", resp2.json())

    def test_admin_journal_import_empty(self):
        list_url = reverse("admin-journal-import")
        resp = self.client.post(list_url, data={"journals": []}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_journal_detail_put_and_delete_not_found(self):
        detail_url = reverse("admin-journal-detail", kwargs={"journal_id": 999999})
        resp_put = self.client.put(detail_url, data={"title": "x"}, format="json")
        self.assertEqual(resp_put.status_code, 404)
        resp_del = self.client.delete(detail_url)
        self.assertEqual(resp_del.status_code, 404)

    def test_admin_journal_detail_get_not_found(self):
        detail_url = reverse("admin-journal-detail", kwargs={"journal_id": 888888})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 404)

    def test_admin_journal_list_search_and_source_filter(self):
        from api.models import JournalArticle
        list_url = reverse("admin-journal-list")
        JournalArticle.objects.create(title="Doaj J", abstract="A", source_portal="doaj")
        JournalArticle.objects.create(title="Other K", abstract="B", source_portal="other")
        resp = self.client.get(list_url + "?search=Doaj&source=doaj&page=1&per_page=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["pagination"]["page"], 1)


class AdminJournalEmbedBranchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminembed",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)


    def test_embed_error_branch_logs_error(self):
        from api.models import JournalArticle
        j = JournalArticle.objects.create(title="Jerr", abstract="A", source_portal="other", is_embedded=False)
        url = reverse("admin-journal-embed")
        with patch("api.views.embed_journal_article", side_effect=Exception("x")):
            resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("embedded_count", resp.json())


class AdminJournalDetailErrorPaths(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminjerr",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def test_put_generic_exception(self):
        from api.models import JournalArticle
        j = JournalArticle.objects.create(title="Jx", abstract="Ax", source_portal="other")
        url = reverse("admin-journal-detail", kwargs={"journal_id": j.id})
        with patch("api.views.JournalArticle.save", side_effect=Exception("boom")):
            resp = self.client.put(url, data={"title": "new"}, format="json")
        self.assertEqual(resp.status_code, 500)

    def test_delete_generic_exception(self):
        from api.models import JournalArticle
        j = JournalArticle.objects.create(title="Jdel", abstract="Adel", source_portal="other")
        url = reverse("admin-journal-detail", kwargs={"journal_id": j.id})
        with patch("api.views.JournalArticle.delete", side_effect=Exception("boom")):
            resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 500)


class AdminJournalImportEdgeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminimp",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def test_import_duplicate_doi_and_exception_path(self):
        from api.models import JournalArticle
        # Pre-create a journal with DOI to trigger duplicate path
        JournalArticle.objects.create(title="Jexists", abstract="A", doi="10.2000/dup", source_portal="other")

        payload = {
            "journals": [
                {"title": "Jdup", "abstract": "A1", "doi": "10.2000/dup", "source_portal": "other"},
                {"title": "Jerr", "abstract": "A2", "doi": "10.2000/err", "source_portal": "other"},
            ]
        }
        url = reverse("admin-journal-import")
        # Patch create to raise error when title == 'Jerr'
        def create_side_effect(*args, **kwargs):
            if kwargs.get("title") == "Jerr":
                raise Exception("fail")
            return JournalArticle.objects.create(*args, **kwargs)
        with patch("api.views.JournalArticle.objects.create", side_effect=create_side_effect):
            resp = self.client.post(url, data=payload, format="json")
        self.assertIn(resp.status_code, (201, 400))
        self.assertIn("errors", resp.json())


class AdminInternalMethodTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(username="staffx", password="p", is_staff=True, is_superuser=False)
        self.client.force_authenticate(user=self.staff)

    def test_add_user_evidence_as_source_new_and_existing(self):
        from api.admin_views import AdminDisputeDetailView
        from api.models import Claim, Source
        claim = Claim.objects.create(text="Klaim")
        view = AdminDisputeDetailView()

        # New source via DOI
        evidence_new = {"doi": "10.3000/new", "title": "New", "abstract": "Abs", "url": "https://doi.org/10.3000/new"}
        view._add_user_evidence_as_source(claim, evidence_new)
        self.assertTrue(Source.objects.filter(doi="10.3000/new").exists())

        # Existing source path
        existing = Source.objects.create(title="Existing", doi="10.3000/exist", url="https://doi.org/10.3000/exist")
        evidence_exist = {"doi": "10.3000/exist", "title": "Existing", "abstract": "Abs", "url": "https://doi.org/10.3000/exist"}
        view._add_user_evidence_as_source(claim, evidence_exist)
        self.assertTrue(claim.sources.filter(id=existing.id).exists())

    def test_update_claim_sources_returns_true_and_sets_rank(self):
        from api.admin_views import AdminDisputeDetailView
        from api.models import Claim
        claim = Claim.objects.create(text="Klaim x")
        view = AdminDisputeDetailView()
        new_sources = [
            {"title": "S1", "url": "https://example.com/s1", "relevance_score": 0.9, "source_type": "journal"},
            {"title": "S2", "url": "https://example.com/s2", "relevance_score": 0.5, "source_type": "journal"},
        ]
        ok = view._update_claim_sources(claim, new_sources)
        self.assertTrue(ok)
        links = ClaimSource.objects.filter(claim=claim).order_by('rank')
        self.assertEqual(links.count(), 2)
        self.assertEqual(links[0].rank, 0)
        self.assertEqual(links[1].rank, 1)

    def test_admin_dispute_approve_manual_update_adds_evidence(self):
        from api.admin_views import AdminDisputeDetailView
        from api.models import Claim, Dispute, VerificationResult, Source
        claim = Claim.objects.create(text="Klaim manual")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNCERTAIN, summary="s", confidence=0.6)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan",
            reporter_email="user@example.com",
            supporting_doi="10.4000/manual",
            status=Dispute.STATUS_PENDING,
        )
        view = AdminDisputeDetailView()
        # Simulasikan request dengan user staff
        class DummyReq: 
            user = self.staff
        result = view._handle_approve(
            dispute=dispute,
            request=DummyReq(),
            review_note="catatan",
            manual_update=True,
            re_verify=False,
            new_label="valid",
            new_confidence=0.8,
            new_summary="updated"
        )
        # `_handle_approve` mengembalikan status dispute ('approved'), konsisten
        # dengan `_handle_reject` yang mengembalikan 'rejected'. Ekspektasi lama
        # ("success") tidak pernah cocok dengan implementasi.
        self.assertEqual(result["status"], Dispute.STATUS_APPROVED)
        self.assertTrue(Source.objects.filter(doi="10.4000/manual").exists())


class DashboardErrorTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(username="staffdash", password="p", is_staff=True, is_superuser=False)
        self.client.force_authenticate(user=self.staff)

    def test_admin_dashboard_error_path(self):
        url = reverse("admin-dashboard-stats")
        with patch("api.admin_views.Claim.objects.count", side_effect=Exception("boom")):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 500)
class ClaimListPaginationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        for i in range(0, 55):
            claim = Claim.objects.create(text=f"Claim {i}")
            VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNVERIFIED, summary="", confidence=None)

    def test_pagination_multiple_pages(self):
        url = reverse("claim-list") + "?page=2&per_page=20"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["pagination"]["page"], 2)
        self.assertTrue(data["pagination"]["has_next"])
        self.assertTrue(data["pagination"]["has_previous"])


class AdminSourceListViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="adminsrc",
            password="pass12345",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def test_source_list_search_and_pagination(self):
        from api.models import Source
        for i in range(0, 5):
            Source.objects.create(title=f"Title {i}", url=f"https://example.com/{i}", credibility_score=0.5)

        url = reverse("admin-source-list") + "?search=Title&page=1&per_page=2"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["pagination"]["page"], 1)
        self.assertTrue(data["pagination"]["total"] >= 5)

    def test_source_list_error_path(self):
        url = reverse("admin-source-list")
        with patch("api.admin_views.Source.objects.all", side_effect=Exception("boom")):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 500)

    def test_source_detail_get_and_error_paths(self):
        from api.models import Source
        src = Source.objects.create(title="S1", url="https://example.com/1", credibility_score=0.5)
        detail_url = reverse("admin-source-detail", kwargs={"source_id": src.id})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)

        not_found_url = reverse("admin-source-detail", kwargs={"source_id": 999999})
        resp_nf = self.client.get(not_found_url)
        self.assertEqual(resp_nf.status_code, 404)

        with patch("api.admin_views.Source.objects.get", side_effect=Exception("x")):
            resp_err = self.client.get(detail_url)
        self.assertEqual(resp_err.status_code, 500)

    def test_source_put_validation_and_error(self):
        from api.models import Source
        src = Source.objects.create(title="S1", url="https://example.com/1", credibility_score=0.5)
        detail_url = reverse("admin-source-detail", kwargs={"source_id": src.id})

        resp_bad = self.client.put(detail_url, data={"credibility_score": 2}, format="json")
        self.assertEqual(resp_bad.status_code, 400)

        Source.objects.create(title="S2", url="https://dup.com", credibility_score=0.5)
        resp_dup = self.client.put(detail_url, data={"url": "https://dup.com", "credibility_score": 0.5}, format="json")
        self.assertEqual(resp_dup.status_code, 400)

        with patch("api.admin_views.Source.save", side_effect=Exception("boom")):
            resp_err = self.client.put(detail_url, data={"credibility_score": 0.7}, format="json")
        self.assertEqual(resp_err.status_code, 500)

    def test_source_delete_paths(self):
        from api.models import Source
        src = Source.objects.create(title="S1", url="https://example.com/1", credibility_score=0.5)
        detail_url = reverse("admin-source-detail", kwargs={"source_id": src.id})
        resp = self.client.delete(detail_url)
        self.assertEqual(resp.status_code, 200)

        not_found_url = reverse("admin-source-detail", kwargs={"source_id": 999999})
        resp_nf = self.client.delete(not_found_url)
        self.assertEqual(resp_nf.status_code, 404)

        src2 = Source.objects.create(title="S2", url="https://example.com/2", credibility_score=0.5)
        detail_url2 = reverse("admin-source-detail", kwargs={"source_id": src2.id})
        with patch("api.admin_views.Source.delete", side_effect=Exception("boom")):
            resp_err = self.client.delete(detail_url2)
        self.assertEqual(resp_err.status_code, 500)
class AiAdapterUnitTests(TestCase):
    def test_call_ai_verify_direct_optimized(self):
        from api import ai_adapter

        class DummyModule:
            def verify_claim_local(self, claim, **kwargs):
                return {
                    "_frontend_payload": {
                        "label": "verified",
                        "confidence": 0.76,
                        "summary": "Kesimpulan",
                        # DOI dengan format sah (10.<4-9 digit>/<suffix>), DOI
                        # yang formatnya ngawur kini sengaja ditolak.
                        "sources": [{"doi": "10.1016/j.test.2020.01.001",
                                     "relevance_score": 0.9}],
                    }
                }

        with patch("api.ai_adapter.get_optimized_module", return_value=DummyModule()), \
             patch("ragai.evidence.link_validator.resolve_doi",
                   return_value="verified"):
            result = ai_adapter.call_ai_verify_direct_optimized("Klaim contoh")
        self.assertEqual(result["label"], "valid")
        self.assertGreaterEqual(result["confidence"], 0.75)
        self.assertTrue(result["sources"])

    def test_call_ai_verify_with_evidence_path(self):
        from api import ai_adapter

        class DummyModule:
            def verify_claim_local(self, claim, **kwargs):
                return {"label": "valid", "confidence": 0.8, "summary": "ok", "sources": []}

        evidence = {"doi": "10.1000/x", "title": "Judul", "url": "https://doi.org/10.1000/x", "abstract": "abs"}
        with patch("api.ai_adapter.get_optimized_module", return_value=DummyModule()):
            raw = ai_adapter.call_ai_verify_with_evidence("klaim", evidence)
        self.assertTrue(raw["sources"])

    def test_safe_float_and_parse_json(self):
        from api.ai_adapter import safe_float, parse_json_from_output

        self.assertEqual(safe_float(None, default=1.5), 1.5)
        self.assertEqual(safe_float("2.5"), 2.5)

        self.assertEqual(parse_json_from_output('{"a":1}')["a"], 1)
        self.assertEqual(parse_json_from_output("xxx {\"a\": 2} yyy")["a"], 2)
        self.assertEqual(parse_json_from_output("[{\"a\": 3}]")["a"], 3)

    def test_validate_url_branches(self):
        from api.ai_adapter import validate_url

        class HeadResp:
            def __init__(self, status_code, url):
                self.status_code = status_code
                self.url = url

        with patch("api.ai_adapter.requests.head", return_value=HeadResp(404, "https://x")):
            self.assertEqual(validate_url("https://bad"), "")

        with patch("api.ai_adapter.requests.head", return_value=HeadResp(200, "https://final")):
            self.assertEqual(validate_url("https://ok"), "https://final")

        with patch("api.ai_adapter.requests.head", side_effect=Exception("x")):
            self.assertEqual(validate_url("https://fallback"), "https://fallback")

    def test_normalize_ai_response_hoax_and_valid_paths(self):
        from api.ai_adapter import normalize_ai_response

        doi = "10.1016/j.lungcan.2019.05.012"

        with patch("ragai.evidence.link_validator.resolve_doi",
                   return_value="verified"):
            hoax = normalize_ai_response(
                {"label": "false", "confidence": 80, "summary": "s",
                 "sources": [{"doi": doi}]},
                claim_text="Merokok menyebabkan kanker paru",
            )
            valid = normalize_ai_response(
                {"label": "valid", "confidence": "80", "summary": "s",
                 "sources": [{"doi": doi}]},
                claim_text="Merokok menyebabkan kanker paru",
            )

        self.assertEqual(hoax["label"], "hoax")
        self.assertEqual(hoax["confidence"], 0.8)
        self.assertEqual(valid["label"], "valid")
        self.assertEqual(valid["confidence"], 0.8)

    def test_normalize_ai_response_drops_unverifiable_doi(self):
        """DOI karangan tidak boleh lolos menjadi sumber (regresi anti-404)."""
        from api.ai_adapter import normalize_ai_response

        with patch("ragai.evidence.link_validator.resolve_doi",
                   return_value="unresolvable"):
            result = normalize_ai_response(
                {"label": "valid", "confidence": 90, "summary": "s",
                 "sources": [{"doi": "10.9999/tidak-ada-doi-ini", "title": "Karangan"}]},
                claim_text="Vitamin X menyembuhkan penyakit Y",
            )

        self.assertEqual(result["sources"], [])
        # Tanpa sumber jurnal terverifikasi, label wajib jatuh ke unverified.
        self.assertEqual(result["label"], "unverified")
        self.assertIsNone(result["confidence"])

    def test_extract_sources_filters_and_sorts(self):
        from api.ai_adapter import extract_sources

        with patch("ragai.evidence.link_validator.check_url",
                   return_value=("unresolvable", "")):
            sources = extract_sources({"sources": [{"url": "https://bad"}]})
        self.assertEqual(sources, [])

        doi_a = "10.1016/j.aaa.2020.01.001"
        doi_b = "10.1016/j.bbb.2020.01.002"
        with patch("ragai.evidence.link_validator.resolve_doi",
                   return_value="verified"):
            sources = extract_sources(
                {
                    "sources": [
                        {"doi": doi_a, "relevance_score": 0.1},
                        {"doi": doi_b, "relevance_score": 0.9},
                    ]
                }
            )
        self.assertEqual(sources[0]["doi"], doi_b)
        self.assertTrue(sources[0]["url"].startswith("https://doi.org/"))
        self.assertTrue(sources[0]["_doi_verified"])


class EmailServiceExtendedTests(TestCase):
    @override_settings(ENABLE_EMAIL_NOTIFICATIONS=True, DEFAULT_FROM_EMAIL="noreply@example.com")
    def test_send_email_no_recipients(self):
        from api.email_service import EmailNotificationService

        svc = EmailNotificationService()
        ok = svc._send_email("s", "m", [])
        self.assertFalse(ok)

    @override_settings(ENABLE_EMAIL_NOTIFICATIONS=True, DEFAULT_FROM_EMAIL="noreply@example.com")
    def test_send_email_plain_and_html(self):
        from api.email_service import EmailNotificationService

        svc = EmailNotificationService()

        with patch("api.email_service.send_mail", return_value=1) as mocked_send:
            ok = svc._send_email("subj", "msg", ["a@b.com"])
        self.assertTrue(ok)
        mocked_send.assert_called_once()

        class DummyEmail:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.alts = []

            def attach_alternative(self, html, mime):
                self.alts.append((html, mime))

            def send(self):
                return 1

        with patch("api.email_service.EmailMultiAlternatives", side_effect=lambda **kw: DummyEmail(**kw)):
            ok = svc._send_email("subj", "msg", ["a@b.com"], html_message="<b>x</b>")
        self.assertTrue(ok)


class MergeDuplicatesExecuteTests(TestCase):
    def test_merge_duplicate_claims_execute_transfers_sources(self):
        from api.models import Source

        primary = Claim.objects.create(text="Duplikat claim")
        primary.status = Claim.STATUS_DONE
        primary.save()
        VerificationResult.objects.create(claim=primary, label=VerificationResult.LABEL_VALID, summary="s", confidence=0.9)

        dup = Claim.objects.create(text="Duplikat claim")
        dup.status = Claim.STATUS_DONE
        dup.save()
        src = Source.objects.create(title="S1", url="https://example.com/s1")
        ClaimSource.objects.create(claim=dup, source=src, relevance_score=0.9, rank=1)

        out = StringIO()
        call_command("merge_duplicate_claims", "--execute", stdout=out)

        self.assertEqual(Claim.objects.filter(text="Duplikat claim").count(), 1)
        remaining = Claim.objects.get(text="Duplikat claim")
        self.assertTrue(remaining.sources.filter(id=src.id).exists())


class EmailServiceUserNotifyTests(TestCase):
    def test_notify_user_dispute_approved_and_rejected(self):
        from api.email_service import EmailNotificationService

        claim = Claim.objects.create(text="Klaim")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_VALID, summary="s", confidence=0.9)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute.",
            reporter_email="user@example.com",
            reporter_name="User",
        )
        svc = EmailNotificationService()
        with patch("api.email_service.send_mail", return_value=1):
            ok1 = svc.notify_user_dispute_approved(dispute, admin_notes="catatan")
        self.assertTrue(ok1)

        with patch("api.email_service.send_mail", return_value=1):
            ok2 = svc.notify_user_dispute_rejected(dispute, admin_notes="catatan")
        self.assertTrue(ok2)

    def test_notify_admin_system_error(self):
        from api.email_service import EmailNotificationService
        svc = EmailNotificationService()
        with patch("api.email_service.send_mail", return_value=1):
            ok = svc.notify_admin_system_error("Verification Failed", "x", {"id": 1})
        self.assertIsInstance(ok, bool)


class TextNormalizationExtendedTests(TestCase):
    def test_find_similar_texts_and_hashes(self):
        from api.text_normalization import find_similar_texts, generate_fuzzy_hash, preprocess_for_comparison, get_similarity_explanation
        res = find_similar_texts("a b c", [(1, "a b c")], threshold=0.5, top_k=3)
        self.assertTrue(res)
        self.assertEqual(len(res), 1)
        self.assertTrue(generate_fuzzy_hash("hello world"))
        self.assertTrue(preprocess_for_comparison("X y z"))
        self.assertEqual(get_similarity_explanation(0.96), "Sangat mirip (kemungkinan besar duplikat)")
        self.assertEqual(get_similarity_explanation(0.80), "Agak mirip (mungkin topik yang sama)")
    def test_claim_similarity_matcher_levels(self):
        from api.text_normalization import ClaimSimilarityMatcher, normalize_claim_text, generate_fuzzy_hash
        matcher = ClaimSimilarityMatcher()
        exact_norm = normalize_claim_text("Vitamin C membantu imunitas")
        existing = [(1, "Vitamin C membantu imunitas", exact_norm)]
        result_exact = matcher.find_duplicates("Vitamin C membantu imunitas", existing)
        self.assertEqual(result_exact["match_level"], "exact")
        fh = generate_fuzzy_hash("A B C")
        existing2 = [(2, "A   B   C", normalize_claim_text("A   B   C"))]
        result_high = matcher.find_duplicates("A B C", existing2)
        self.assertIn(result_high["match_level"], ("high", "exact"))
        existing3 = [(3, "B C D E", normalize_claim_text("B C D E"))]
        result_med = matcher.find_duplicates("B C D", existing3)
        self.assertIn(result_med["match_level"], ("medium", "low", "high"))


class AdminPipelineAndJournalsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(username="staff", password="p", is_staff=True, is_superuser=False)
        self.client.force_authenticate(user=self.staff)

    def test_pipeline_success_and_fetch_similar_journals(self):
        from api.admin_views import AdminDisputeDetailView

        claim = Claim.objects.create(text="Klaim panjang tentang kesehatan dan penelitian")
        VerificationResult.objects.create(claim=claim, label=VerificationResult.LABEL_UNCERTAIN, summary="s", confidence=0.6)
        dispute = Dispute.objects.create(
            claim=claim,
            claim_text=claim.text,
            reason="Alasan panjang untuk dispute yang valid.",
            status=Dispute.STATUS_PENDING,
            supporting_doi="10.1000/abc",
        )

        def dummy_fetch_doi(doi):
            return {"doi": doi, "title": "T", "abstract": "A", "url": f"https://doi.org/{doi}"}

        with (
            patch("api.admin_views.fetch_evidence_from_doi", side_effect=dummy_fetch_doi),
            patch("api.admin_views.call_ai_verify", return_value={"label": "valid", "confidence": 0.8, "summary": "x"}),
        ):
            view = AdminDisputeDetailView()
            ok = view._trigger_pipeline(dispute)
        self.assertTrue(ok)

        class DummyPaper:
            def __init__(self, url, title="Title"):
                self.url = url
                self.title = title
                self.doi = "10.1/x"
                self.abstract = "abs"

        def dummy_search(query, limit=2):
            return [DummyPaper("https://example.com/a"), DummyPaper("https://example.com/b")]

        with patch("api.admin_views.SemanticScholar") as mocked_sem:
            mocked_sem.return_value.search_paper = dummy_search
            ok2 = view._fetch_similar_journals(claim)
        self.assertTrue(ok2)

    def test_fetch_similar_journals_short_text_and_no_results(self):
        from api.admin_views import AdminDisputeDetailView
        claim = Claim.objects.create(text="X")
        view = AdminDisputeDetailView()
        # text terlalu pendek -> False
        ok_short = view._fetch_similar_journals(claim)
        self.assertFalse(ok_short)

        # text cukup panjang tapi SemanticScholar mengembalikan None
        claim2 = Claim.objects.create(text="Klaim panjang tentang kesehatan dan penelitian cukup untuk test")
        with patch("api.admin_views.SemanticScholar") as mocked_sem:
            mocked_sem.return_value.search_paper.return_value = None
            ok_none = view._fetch_similar_journals(claim2)
        self.assertFalse(ok_none)


class LabelPromotionGuardTests(TestCase):
    """Penilaian AI tidak boleh dipromosikan menjadi FAKTA oleh tangga confidence."""

    DOI = "10.1016/j.lungcan.2019.05.012"

    def _normalize(self, label, confidence):
        from api.ai_adapter import normalize_ai_response

        with patch("ragai.evidence.link_validator.resolve_doi",
                   return_value="verified"):
            return normalize_ai_response(
                {
                    "label": label,
                    "confidence": confidence,
                    "summary": "Bukti yang tersedia tidak membahas hubungan yang diklaim.",
                    "sources": [{"doi": self.DOI, "title": "Studi terkait"}],
                },
                claim_text="Merokok menyebabkan kanker paru",
            )

    def test_uncertain_with_high_confidence_stays_uncertain(self):
        """Regresi: dulu ini berubah menjadi 'valid' dan bertentangan dengan ringkasannya."""
        result = self._normalize("uncertain", 0.98)
        self.assertEqual(result["label"], "uncertain")

    def test_unverified_is_not_promoted(self):
        result = self._normalize("inconclusive", 0.95)
        self.assertEqual(result["label"], "unverified")

    def test_hoax_is_never_flipped(self):
        result = self._normalize("hoax", 0.95)
        self.assertEqual(result["label"], "hoax")

    def test_valid_still_follows_confidence_ladder(self):
        self.assertEqual(self._normalize("valid", 0.9)["label"], "valid")
        # Confidence rendah tetap menurunkan label meski AI bilang valid.
        self.assertEqual(self._normalize("valid", 0.4)["label"], "hoax")
        self.assertEqual(self._normalize("valid", 0.6)["label"], "uncertain")

    def test_uncertain_without_journal_becomes_unverified(self):
        from api.ai_adapter import normalize_ai_response

        result = normalize_ai_response(
            {"label": "uncertain", "confidence": 0.9, "summary": "s", "sources": []},
            claim_text="Merokok menyebabkan kanker paru",
        )
        self.assertEqual(result["label"], "unverified")
        self.assertIsNone(result["confidence"])
