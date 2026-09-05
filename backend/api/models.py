import logging

from django.db import models
from .text_normalization import normalize_claim_text, generate_semantic_hash
from .version import VERIFICATION_LOGIC_VERSION


# menyimpan sumber referensi seperti doi, url
class Source(models.Model):
    title = models.CharField(max_length=500)
    doi = models.CharField(max_length=255, blank=True, null=True)
    url = models.URLField(blank=True, null=True)
    authors = models.TextField(blank=True, null=True)
    publisher = models.CharField(max_length=255, blank=True, null=True)
    published_date = models.DateField(blank=True, null=True)
    credibility_score = models.FloatField(default=0.5, help_text="Skor kredibilitas 0.0 - 1.0")

    SOURCE_TYPE_CHOICES = [
        ('website', 'Website'),
        ('journal', 'Journal'),
        ('news', 'News'),
        ('government', 'Government'),
        ('organization', 'Organization'),
        ('other', 'Other'),
    ]
    source_type = models.CharField(
        max_length=50, 
        choices=SOURCE_TYPE_CHOICES, 
        default='website',
        help_text="Tipe sumber"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.title} ({self.doi or self.url or 'no-id'})"
        
# menyimpan klaim yang dikirim untuk diverifikasi
class Claim(models.Model):
    text = models.TextField()
    text_normalized = models.TextField(blank=True, null=True)
    text_hash = models.CharField(max_length=64, db_index=True, null=True, blank=True)

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_DONE = 'done'
    STATUS_DISPUTED = 'disputed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_DONE, 'Done'),
        (STATUS_DISPUTED, 'Disputed'),
    ]

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sources = models.ManyToManyField(Source, through='ClaimSource', blank=True)

    def save(self, *args, **kwargs):
        # Auto-generate normalized text & hash saat save
        self.text_normalized = normalize_claim_text(self.text)
        self.text_hash = generate_semantic_hash(self.text)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Claim #{self.pk} - {self.text[:50]}...'

    class Meta:
        indexes = [
            models.Index(fields=['text_hash']),
            models.Index(fields=['text_normalized']),
        ]
    
# Model hubungan antara claim dan sumber
class ClaimSource(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE)
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    relevance_score = models.FloatField(default=0.0)  
    excerpt = models.TextField(blank=True, null=True)  
    rank = models.IntegerField(default=0)  

    class Meta:
        unique_together = ('claim', 'source')
        ordering = ['rank']
        indexes = [
            models.Index(fields=['claim', 'source']),
            models.Index(fields=['rank']),
        ]

    def __str__(self):
        return f'ClaimSource: Claim #{self.claim_id} - Source #{self.source_id}'
    
    def save(self, *args, **kwargs):
        """
            Override save to handle duplicates gracefully.
        """
        try:
            super().save(*args, **kwargs)
        except Exception as e:
            if 'unique constraint' in str(e).lower():
                # log and skip duplicate
                logger = logging.getLogger(__name__)
                logger.warning(
                    f"Skipping duplicate ClaimSource: "
                    f"Claim_id={self.claim_id}, Source_id={self.source_id}"
                )
            else:
                raise e

# Model untuk menyimpan hasil verifikasi klaim untuk satu klaim
class VerificationResult(models.Model):
    # Label hasil - UPDATED LABELS
    LABEL_VALID = 'valid'
    LABEL_HOAX = 'hoax'
    LABEL_UNCERTAIN = 'uncertain'
    LABEL_UNVERIFIED = 'unverified'
    
    LABEL_CHOICES = [
        (LABEL_VALID, 'FAKTA'),  # Changed from 'Valid'
        (LABEL_HOAX, 'HOAX'),    # Remains same
        (LABEL_UNCERTAIN, 'TIDAK PASTI'),  # Changed from 'Tidak Tentu'
        (LABEL_UNVERIFIED, 'TIDAK TERVERIFIKASI'),  # Changed from 'Tidak Terverifikasi'
    ]
    
    claim = models.OneToOneField(Claim, on_delete=models.CASCADE, related_name='verification_result')
    label = models.CharField(
        max_length=32,
        choices=LABEL_CHOICES,
        default=LABEL_UNVERIFIED,
    )
    summary = models.TextField(blank=True, null=True)
    
    # IMPORTANT: Confidence can be NULL for UNVERIFIED claims
    confidence = models.FloatField(default=0.0, null=True, blank=True)
    
    reviewer_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    logic_version = models.CharField(
        max_length=32, default=VERIFICATION_LOGIC_VERSION, null=True,
        help_text="Versi mesin yang menghasilkan penilaian ini. Hasil dari versi "
                  "lama tidak disajikan dari cache.")

    def confidence_percent(self):
        """Return confidence as percentage, or None if unverified."""
        if self.label == self.LABEL_UNVERIFIED or self.confidence is None:
            return None
        return round(self.confidence * 100, 2)

    def determine_label_from_confidence(self, has_sources=True, has_journal=False):
        """
        Menentukan label berdasarkan confidence score dan sumber.
        
        Rules:
        - TIDAK TERVERIFIKASI: tidak ada sumber atau bukan topik kesehatan
        - FAKTA: confidence >= 0.75 dengan sumber jurnal
        - HOAX: confidence <= 0.55 dengan sumber jurnal
        - TIDAK PASTI: 0.55 < confidence < 0.75 dengan sumber jurnal
        """
        if not has_sources or not has_journal:
            return self.LABEL_UNVERIFIED
        
        if self.confidence is None:
            return self.LABEL_UNVERIFIED
        
        if self.confidence >= 0.75:
            return self.LABEL_VALID
        elif self.confidence <= 0.55:
            return self.LABEL_HOAX
        else:  # 0.55 < confidence < 0.75
            return self.LABEL_UNCERTAIN

    def save(self, *args, **kwargs):
        """Save without overriding label/confidence coming from AI.

        If you explicitly want to auto-derive the label from confidence and
        attached sources, call save(auto_label=True).
        """
        auto_label = kwargs.pop('auto_label', False)

        if auto_label and not self.pk:  # Only on creation when explicitly requested
            has_sources = self.claim.sources.exists() if self.claim else False
            has_journal = False
            if has_sources:
                # Check if any source is a journal (has DOI)
                has_journal = self.claim.sources.filter(
                    models.Q(doi__isnull=False) | models.Q(source_type='journal')
                ).exists()

            self.label = self.determine_label_from_confidence(has_sources, has_journal)

            # Set confidence to NULL for unverified
            if self.label == self.LABEL_UNVERIFIED:
                self.confidence = None

        super().save(*args, **kwargs)
        
    def __str__(self):
        conf_str = f"{self.confidence:.2f}" if self.confidence is not None else "N/A"
        return f'Verification Result for Claim #{self.claim_id}: {self.get_label_display()} ({conf_str})'

# Model laporan hasil verifikasi klaim oleh user
class Dispute(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending Review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]
    
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, null=True, blank=True)
    claim_text = models.TextField(help_text="Teks klaim yang dilaporkan")
    reason = models.TextField(help_text="Alasan pelaporan")
    
    reporter_name = models.CharField(max_length=255, blank=True, default='Anonymous')
    reporter_email = models.EmailField(blank=True, default='')
    
    supporting_doi = models.CharField(max_length=500, blank=True, default='')
    supporting_url = models.URLField(blank=True, default='')
    supporting_file = models.FileField(upload_to='disputes/', blank=True, null=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewed = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    
    # Menyimpan hasil verifikasi original sebelum dispute
    original_label = models.CharField(max_length=50, blank=True, default='')
    original_confidence = models.FloatField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Dispute'
        verbose_name_plural = 'Disputes'
    
    def __str__(self):
        return f"Dispute #{self.id} - {self.status}"

# Model untuk menyimpan laporan dari user
class UserReport(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending Review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    claim_text = models.TextField(help_text="Teks klaim yang dilaporkan oleh user")
    supporting_doi = models.CharField(max_length=500, blank=True, default='', help_text="DOI link sebagai bukti")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    # Link to the processed claim once approved
    processed_claim = models.ForeignKey(Claim, on_delete=models.SET_NULL, null=True, blank=True, related_name='user_reports')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'User Report'
        verbose_name_plural = 'User Reports'

    def __str__(self):
        return f"User Report #{self.id} - {self.claim_text[:50]}... ({self.status})"

# Model untuk FAQ dinamis
class FAQItem(models.Model):
    question = models.CharField(max_length=500)
    answer = models.TextField()
    order = models.IntegerField(default=0)  
    published = models.BooleanField(default=True)

    def __str__(self):
        return f'FAQ: {self.question[:60]}'

class JournalArticle(models.Model):
    """
        Model untuk menyimpan jurnal indonesia yang diinput admin
    """

    SOURCE_CHOICE = [
        ('sinta', 'SINTA'),
        ('garuda', 'Garuda'),
        ('doaj', 'DOAJ'),
        ('google_scholar', 'Google Scholar'),
        ('other', 'Other'),
    ]

    title = models.CharField(max_length=1000)
    abstract = models.TextField()
    authors = models.TextField(blank=True, null=True)
    doi = models.CharField(max_length=255, blank=True, null=True, unique=True)
    url = models.URLField(blank=True, null=True)
    publisher = models.CharField(max_length=500, blank=True, null=True)
    journal_name = models.CharField(max_length=500, blank=True, null=True)
    published_date = models.DateField(blank=True, null=True)

    source_portal = models.CharField(
        max_length=50,
        choices=SOURCE_CHOICE,
        default='other'
    )

    # Untuk RAG Embedding
    embedding = models.TextField(blank=True, null=True)
    is_embedded = models.BooleanField(default=False)

    # Metadata
    credibility_score = models.FloatField(blank=True, null=True)
    keywords = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['doi']),
            models.Index(fields=['title']),
            models.Index(fields=['is_embedded']),
        ]

    def __str__(self):
        return f"{self.title[:80]}... ({self.source_portal})"


# ============================================================================
# Health Intelligence Engine — model TAMBAHAN (additive, §23)
#
# Semua model di bawah ini BARU. Tidak ada satu pun field/tabel Healthify yang
# sudah ada diubah atau dihapus. Healthify tetap berjalan penuh tanpa tabel ini
# (dipakai hanya oleh endpoint /api/v1/intelligence/*).
# ============================================================================

class ConversationSession(models.Model):
    """Sesi percakapan multi-turn (dipakai HealthTalk, opsional untuk Healthify)."""

    STATUS_ACTIVE = 'active'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_CLOSED, 'Closed'),
    ]

    session_id = models.CharField(
        max_length=128, unique=True, db_index=True,
        help_text="ID sesi dari consumer (mis. HealthTalk). Unik lintas consumer."
    )
    consumer = models.CharField(
        max_length=64, default='healthify',
        help_text="Nama consumer: healthify | healthtalk | <lainnya>"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    # Snapshot HealthContext terakhir (JSON string, portabel lintas backend DB)
    health_context = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['session_id']),
            models.Index(fields=['consumer', 'status']),
        ]

    def __str__(self):
        return f"ConversationSession {self.session_id} ({self.consumer})"


class ConversationMessage(models.Model):
    """Satu giliran percakapan dalam sebuah sesi."""

    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_CHOICES = [
        (ROLE_USER, 'User'),
        (ROLE_ASSISTANT, 'Assistant'),
    ]

    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name='messages'
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_USER)
    content = models.TextField()
    intent = models.CharField(max_length=48, blank=True, default='')
    evidence_status = models.CharField(max_length=32, blank=True, default='')
    safety_decision = models.CharField(max_length=16, blank=True, default='')
    # Referensi evidence yang dipakai untuk jawaban ini (JSON string)
    evidence_refs = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self):
        return f"[{self.role}] {self.content[:60]}"


class ConsultationSummary(models.Model):
    """Ringkasan terstruktur hasil sebuah sesi konsultasi (§19)."""

    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name='summaries'
    )
    chief_complaint = models.TextField(blank=True, default='')
    # Payload lengkap summary (JSON string) — termasuk provenance tiap bagian
    payload = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Consultation summaries'

    def __str__(self):
        return f"Summary for {self.session.session_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class ApiAccessRequest(models.Model):
    """
    Permintaan akses API dari pengembang luar (§ dokumentasi publik).

    Diisi lewat formulir di halaman dokumentasi. Baris ini bukan kunci: ia
    hanya catatan permintaan. Kunci diterbitkan terpisah oleh operator dengan
    `python manage.py issue_api_key`, supaya penerbitan tetap keputusan manusia
    dan tidak bisa dipicu sendiri oleh pengisi formulir.
    """

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Menunggu peninjauan'),
        (STATUS_APPROVED, 'Disetujui'),
        (STATUS_REJECTED, 'Ditolak'),
    ]

    name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254)
    organization = models.CharField(max_length=200, blank=True, default='')
    use_case = models.TextField()
    expected_volume = models.CharField(max_length=120, blank=True, default='')

    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default=STATUS_PENDING, db_index=True)
    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'API access request'

    def __str__(self):
        return f"{self.email} ({self.status})"


class IntelligenceApiKey(models.Model):
    """
    Kunci API untuk endpoint Intelligence.

    Kunci TIDAK disimpan dalam bentuk aslinya. Yang tersimpan hanya SHA-256
    miliknya, sehingga bocornya isi database tidak membocorkan kunci yang masih
    berlaku. Nilai aslinya hanya ditampilkan sekali, saat diterbitkan.

    Satu konsumen boleh punya banyak kunci sekaligus: satu per lingkungan
    (produksi, staging), atau satu per aplikasi, sehingga satu kunci dapat
    dicabut tanpa mematikan yang lain. Kunci dari variabel lingkungan
    `INTELLIGENCE_API_KEYS` tetap berlaku berdampingan dengan tabel ini.
    """

    consumer = models.CharField(max_length=100, db_index=True)
    label = models.CharField(max_length=200, blank=True, default='',
                             help_text='Penanda bebas, mis. "backend produksi".')
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(
        max_length=16, blank=True, default='',
        help_text='Beberapa karakter awal kunci, untuk mengenalinya tanpa '
                  'menyimpan nilai aslinya.')

    rate = models.CharField(
        max_length=32, blank=True, default='',
        help_text='Batas laju khusus, mis. "120/min". Kosong = batas bawaan.')

    is_active = models.BooleanField(default=True, db_index=True)
    request_ref = models.ForeignKey(
        ApiAccessRequest, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issued_keys')

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Intelligence API key'

    def __str__(self):
        state = 'aktif' if self.is_active else 'dicabut'
        return f"{self.consumer} · {self.key_prefix}… ({state})"

    @staticmethod
    def hash_key(raw_key: str) -> str:
        import hashlib

        return hashlib.sha256((raw_key or '').strip().encode('utf-8')).hexdigest()
