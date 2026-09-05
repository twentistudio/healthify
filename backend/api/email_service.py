import logging
from dotenv import load_dotenv
from typing import List, Optional, Dict, Any
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils import timezone
from .models import Dispute, Claim

load_dotenv()

logger = logging.getLogger(__name__)

class EmailNotificationService:
    """Service untuk mengirim email notifications."""
    
    def __init__(self):
        self.enabled = getattr(settings, 'ENABLE_EMAIL_NOTIFICATIONS', True)
        self.from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
        self.from_name = getattr(settings, 'NOTIFICATION_FROM_NAME', 'Healthify')
        self.admin_emails = getattr(settings, 'ADMIN_NOTIFICATION_EMAILS', [])
    
    def _build_from_header(self) -> str:
        """Build email from header dengan format: Name <email>"""
        return f"{self.from_name} <{self.from_email}>"
    
    def _send_email(self, subject: str, message: str, recipient_list: List[str], 
                   html_message: Optional[str] = None) -> bool:
        """
        Internal method untuk mengirim email.
        
        Returns:
            bool: True jika berhasil, False jika gagal
        """
        if not self.enabled:
            logger.info(f"[EMAIL] Notifications disabled. Skipping email: {subject}")
            return False
        
        if not recipient_list:
            logger.warning(f"[EMAIL] No recipients for: {subject}")
            return False
        
        try:
            if html_message:
                email = EmailMultiAlternatives(
                    subject=subject,
                    body=message,
                    from_email=self._build_from_header(),
                    to=recipient_list
                )
                email.attach_alternative(html_message, "text/html")
                email.send()
            else:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=self._build_from_header(),
                    recipient_list=recipient_list,
                    fail_silently=False,
                )
            
            logger.info(f"[EMAIL] Sent to {', '.join(recipient_list)}: {subject}")
            return True
            
        except Exception as e:
            logger.error(f"[EMAIL] Failed to send '{subject}': {str(e)}", exc_info=True)
            return False
    
    # ADMIN NOTIFICATIONS
    
    def notify_admin_new_dispute(self, dispute: Dispute) -> bool:
        """
        Kirim email ke admin ketika ada dispute baru.
        
        Args:
            dispute: Dispute object yang baru dibuat
            
        Returns:
            bool: Success status
        """
        if not self.admin_emails:
            logger.warning("[EMAIL] No admin emails configured")
            return False
        
        subject = f"🚨 New Dispute #{dispute.id} - Review Required"
        
        # Plain text version
        message = f"""
New Dispute Submitted - Action Required

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DISPUTE DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Dispute ID: #{dispute.id}
Status: {dispute.status.upper()}
Created: {dispute.created_at.strftime('%Y-%m-%d %H:%M:%S')}

Reporter Information:
- Name: {dispute.reporter_name or 'Anonymous'}
- Email: {dispute.reporter_email or 'Not provided'}

Claim Text:
"{dispute.claim_text[:200]}{'...' if len(dispute.claim_text) > 200 else ''}"

User Feedback:
{dispute.reason}

Supporting Evidence:
- DOI: {dispute.supporting_doi or 'None'}
- URL: {dispute.supporting_url or 'None'}
- File: {'Yes' if dispute.supporting_file else 'No'}

Original Verification:
- Label: {dispute.original_label or 'N/A'}
- Confidence: {f"{dispute.original_confidence * 100:.1f}%" if dispute.original_confidence else 'N/A'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTION REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Please review this dispute in the admin panel:
{settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'localhost:8000'}/admin/disputes/{dispute.id}

Best regards,
Healthify System
        """
        
        # HTML version (optional, lebih bagus)
        html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; background: #f9f9f9;">
                <div style="background: #dc2626; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                    <h2 style="margin: 0;">🚨 New Dispute #{dispute.id}</h2>
                    <p style="margin: 5px 0 0 0;">Review Required</p>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 0 0 8px 8px;">
                    <h3 style="color: #dc2626; border-bottom: 2px solid #dc2626; padding-bottom: 10px;">
                        Dispute Details
                    </h3>
                    
                    <table style="width: 100%; margin: 15px 0;">
                        <tr>
                            <td style="padding: 8px; font-weight: bold; width: 150px;">Dispute ID:</td>
                            <td style="padding: 8px;">#{dispute.id}</td>
                        </tr>
                        <tr style="background: #f9f9f9;">
                            <td style="padding: 8px; font-weight: bold;">Status:</td>
                            <td style="padding: 8px;">
                                <span style="background: #fbbf24; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px;">
                                    {dispute.status.upper()}
                                </span>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; font-weight: bold;">Created:</td>
                            <td style="padding: 8px;">{dispute.created_at.strftime('%Y-%m-%d %H:%M:%S')}</td>
                        </tr>
                        <tr style="background: #f9f9f9;">
                            <td style="padding: 8px; font-weight: bold;">Reporter:</td>
                            <td style="padding: 8px;">{dispute.reporter_name or 'Anonymous'}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; font-weight: bold;">Email:</td>
                            <td style="padding: 8px;">{dispute.reporter_email or 'Not provided'}</td>
                        </tr>
                    </table>
                    
                    <div style="background: #eff6ff; border-left: 4px solid #3b82f6; padding: 15px; margin: 15px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #1e40af;">Claim Text</h4>
                        <p style="margin: 0; font-style: italic;">
                            "{dispute.claim_text[:200]}{'...' if len(dispute.claim_text) > 200 else ''}"
                        </p>
                    </div>
                    
                    <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 15px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #92400e;">User Feedback</h4>
                        <p style="margin: 0;">
                            {dispute.reason}
                        </p>
                    </div>
                    
                    <div style="margin: 20px 0; text-align: center;">
                        <a href="http://{settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'localhost:8000'}/admin/disputes/{dispute.id}" 
                           style="display: inline-block; background: #dc2626; color: white; padding: 12px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                            Review Dispute →
                        </a>
                    </div>
                    
                    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
                    
                    <p style="color: #6b7280; font-size: 12px; text-align: center; margin: 0;">
                        This is an automated notification from Healthify System.<br>
                        Please do not reply to this email.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return self._send_email(
            subject=subject,
            message=message,
            recipient_list=self.admin_emails,
            html_message=html_message
        )
    
    def notify_admin_system_error(self, error_type: str, error_message: str, 
                                  context: Optional[Dict[str, Any]] = None) -> bool:
        """
        Kirim email ke admin ketika terjadi system error.
        
        Args:
            error_type: Tipe error (e.g., 'Verification Failed', 'Database Error')
            error_message: Pesan error detail
            context: Additional context information
            
        Returns:
            bool: Success status
        """
        if not self.admin_emails:
            return False
        
        subject = f"⚠️ System Error: {error_type}"
        
        context_str = ""
        if context:
            context_str = "\n\nContext:\n" + "\n".join([f"- {k}: {v}" for k, v in context.items()])
        
        message = f"""
System Error Detected

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERROR DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type: {error_type}
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

Error Message:
{error_message}
{context_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Please investigate this issue as soon as possible.

Best regards,
Healthify System
        """
        
        return self._send_email(
            subject=subject,
            message=message,
            recipient_list=self.admin_emails
        )
    
    # USER NOTIFICATIONS
    
    def notify_user_dispute_approved(self, dispute: Dispute, admin_notes: str = "") -> bool:
        """Kirim email ke user ketika dispute di-approve."""
        if not dispute.reporter_email:
            logger.warning(f"[EMAIL] No reporter email for dispute {dispute.id}")
            return False
        
        subject = f"✅ Laporan Anda Diterima - Dispute #{dispute.id}"
        
        # Get claim verification if available
        claim_info = ""
        if dispute.claim and hasattr(dispute.claim, 'verification_result'):
            vr = dispute.claim.verification_result
            claim_info = f"""
            
Hasil Verifikasi Terbaru:
- Klaim: {dispute.claim_text}
- Status: DITERIMA
- Label: {vr.get_label_display()}
- Tingkat Keyakinan: {vr.confidence_percent()}%
- Ringkasan: {vr.summary[:300]}...

Catatan Admin:
{admin_notes}

Terima kasih telah berkontribusi untuk memerangi misinformasi kesehatan.
            """
        
        message = f"""
    Halo {dispute.reporter_name or 'User'},

    Terima kasih telah melaporkan klaim yang menurutmu tidak akurat. Tim Healthify telah meninjau laporan Anda dan keputusan telah dibuat.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    STATUS: ✅ DITERIMA
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Dispute ID: #{dispute.id}
    Klaim: "{dispute.claim_text[:200]}{'...' if len(dispute.claim_text) > 200 else ''}"
    Tanggal Review: {dispute.reviewed_at.strftime('%d %B %Y %H:%M') if dispute.reviewed_at else 'Hari ini'}
    {claim_info}

    Catatan Admin:
    {admin_notes or 'Laporan Anda telah dipertimbangkan dalam proses verifikasi.'}

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Kontribusi Anda membantu kami meningkatkan akurasi Healthify.
    Terima kasih telah menjadi bagian dari komunitas kami! 🙏

    Best regards,
    Tim Healthify
        """
        
        html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; background: #f9f9f9;">
                <div style="background: #10b981; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                    <h2 style="margin: 0;">✅ Laporan Anda Diterima!</h2>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 0 0 8px 8px;">
                    <p>Halo {dispute.reporter_name or 'User'},</p>
                    
                    <p>Terima kasih telah melaporkan klaim yang menurutmu tidak akurat. 
                    Tim Healthify telah meninjau laporan Anda.</p>
                    
                    <div style="background: #d1fae5; border-left: 4px solid #10b981; padding: 15px; margin: 20px 0;">
                        <h3 style="margin: 0 0 10px 0; color: #065f46;">Status: Diterima ✅</h3>
                        <p style="margin: 0; font-size: 14px;">
                            Dispute ID: #{dispute.id}<br>
                            Tanggal Review: {dispute.reviewed_at.strftime('%d %B %Y') if dispute.reviewed_at else 'Hari ini'}
                        </p>
                    </div>
                    
                    <div style="background: #f3f4f6; padding: 15px; border-radius: 6px; margin: 20px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #1f2937;">Klaim yang Dilaporkan:</h4>
                        <p style="margin: 0; font-style: italic;">
                            "{dispute.claim_text[:200]}{'...' if len(dispute.claim_text) > 200 else ''}"
                        </p>
                    </div>
                    
                    {f'''
                    <div style="background: #eff6ff; border-left: 4px solid #3b82f6; padding: 15px; margin: 20px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #1e40af;">Hasil Verifikasi Terbaru:</h4>
                        <table style="width: 100%; font-size: 14px;">
                            <tr>
                                <td style="padding: 5px; color: #6b7280;">Label:</td>
                                <td style="padding: 5px; font-weight: bold;">{dispute.claim.verification_result.get_label_display() if hasattr(dispute.claim, 'verification_result') else 'N/A'}</td>
                            </tr>
                            <tr style="background: #f9fafb;">
                                <td style="padding: 5px; color: #6b7280;">Confidence:</td>
                                <td style="padding: 5px; font-weight: bold;">{dispute.claim.verification_result.confidence_percent() if hasattr(dispute.claim, 'verification_result') else 'N/A'}%</td>
                            </tr>
                        </table>
                    </div>
                    ''' if dispute.claim and hasattr(dispute.claim, 'verification_result') else ''}
                    
                    {f'''
                    <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #92400e;">Catatan Admin:</h4>
                        <p style="margin: 0; font-size: 14px;">{admin_notes or 'Laporan Anda telah dipertimbangkan dalam proses verifikasi.'}</p>
                    </div>
                    ''' if admin_notes else ''}
                    
                    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
                    
                    <p style="color: #10b981; font-weight: bold;">
                        Kontribusi Anda membantu kami meningkatkan akurasi Healthify! 🙏
                    </p>
                    
                    <p style="color: #6b7280; font-size: 12px; text-align: center; margin-top: 20px;">
                        Terima kasih telah menjadi bagian dari komunitas kami.<br>
                        Tim Healthify
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return self._send_email(
            subject=subject,
            message=message,
            recipient_list=[dispute.reporter_email],
            html_message=html_message
        )

    def notify_user_dispute_rejected(self, dispute: Dispute, admin_notes: str = "") -> bool:
        """Kirim email ke user ketika dispute di-reject."""
        if not dispute.reporter_email:
            logger.warning(f"[EMAIL] No reporter email for dispute {dispute.id}")
            return False
        
        subject = f"📋 Update Laporan Anda - Dispute #{dispute.id}"
        
        reason = admin_notes or "Setelah tinjauan mendalam, tim kami memutuskan untuk mempertahankan verification result original."
        
        message = f"""
    Halo {dispute.reporter_name or 'User'},

    Terima kasih atas laporan Anda mengenai verifikasi klaim. Kami telah meninjau laporan dengan cermat.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    STATUS: TIDAK DITERIMA
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Dispute ID: #{dispute.id}
    Klaim: "{dispute.claim_text[:200]}{'...' if len(dispute.claim_text) > 200 else ''}"
    Tanggal Review: {dispute.reviewed_at.strftime('%d %B %Y %H:%M') if dispute.reviewed_at else 'Hari ini'}

    Alasan:
    {reason}

    Original Verification Result (Tetap Berlaku):
    - Label: {dispute.original_label}
    - Confidence: {f"{dispute.original_confidence * 100:.1f}%" if dispute.original_confidence else 'N/A'}

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Jika Anda memiliki bukti tambahan yang kuat, silakan ajukan laporan baru dengan evidence yang lebih terperinci.

    Terima kasih atas partisipasi Anda!

    Best regards,
    Tim Healthify
        """
        
        html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; background: #f9f9f9;">
                <div style="background: #6b7280; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                    <h2 style="margin: 0;">📋 Update Laporan Anda</h2>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 0 0 8px 8px;">
                    <p>Halo {dispute.reporter_name or 'User'},</p>
                    
                    <p>Terima kasih atas laporan Anda mengenai verifikasi klaim. 
                    Kami telah meninjau laporan dengan cermat.</p>
                    
                    <div style="background: #f3f4f6; border-left: 4px solid #6b7280; padding: 15px; margin: 20px 0;">
                        <h3 style="margin: 0 0 10px 0; color: #374151;">Status: Tidak Diterima</h3>
                        <p style="margin: 0; font-size: 14px;">
                            Dispute ID: #{dispute.id}<br>
                            Tanggal Review: {dispute.reviewed_at.strftime('%d %B %Y') if dispute.reviewed_at else 'Hari ini'}
                        </p>
                    </div>
                    
                    <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #92400e;">Alasan:</h4>
                        <p style="margin: 0; font-size: 14px;">
                            {reason}
                        </p>
                    </div>
                    
                    <div style="background: #f0f9ff; border-left: 4px solid #0284c7; padding: 15px; margin: 20px 0;">
                        <h4 style="margin: 0 0 10px 0; color: #0c4a6e;">Verification Result Original (Tetap Berlaku):</h4>
                        <table style="width: 100%; font-size: 14px;">
                            <tr>
                                <td style="padding: 5px; color: #6b7280;">Label:</td>
                                <td style="padding: 5px; font-weight: bold;">{dispute.original_label.upper()}</td>
                            </tr>
                            <tr style="background: #f9fafb;">
                                <td style="padding: 5px; color: #6b7280;">Confidence:</td>
                                <td style="padding: 5px; font-weight: bold;">{f"{dispute.original_confidence * 100:.1f}%" if dispute.original_confidence else 'N/A'}</td>
                            </tr>
                        </table>
                    </div>
                    
                    <p style="color: #6b7280; font-size: 14px; font-style: italic;">
                        💡 Jika Anda memiliki bukti tambahan yang kuat, silakan ajukan laporan baru 
                        dengan evidence yang lebih terperinci.
                    </p>
                    
                    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
                    
                    <p style="color: #6b7280; font-size: 12px; text-align: center;">
                        Terima kasih atas partisipasi Anda dalam komunitas Healthify.<br>
                        Tim Healthify
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return self._send_email(
            subject=subject,
            message=message,
            recipient_list=[dispute.reporter_email],
            html_message=html_message
        )

    # ENGINE PUBLIK (ragai)

    def _engine_from_header(self) -> str:
        """
        Pengirim untuk surat yang berasal dari engine publik.

        Engine punya nama sendiri; pemohon tidak menerima surat atas nama
        produk Healthify.
        """
        name = getattr(settings, 'ENGINE_BRAND_NAME', 'ragai')
        return f"{name} <{self.from_email}>"

    def _send_as_engine(self, subject: str, message: str, recipients: List[str],
                        html_message: Optional[str] = None) -> bool:
        if not self.enabled:
            logger.info("[EMAIL] Notifikasi dimatikan, dilewati: %s", subject)
            return False
        if not recipients:
            logger.warning("[EMAIL] Tidak ada penerima untuk: %s", subject)
            return False
        try:
            email = EmailMultiAlternatives(
                subject=subject, body=message,
                from_email=self._engine_from_header(), to=recipients,
            )
            if html_message:
                email.attach_alternative(html_message, "text/html")
            email.send()
            logger.info("[EMAIL] Terkirim ke %s: %s", ", ".join(recipients), subject)
            return True
        except Exception as exc:
            logger.error("[EMAIL] Gagal mengirim '%s': %s", subject, exc, exc_info=True)
            return False

    def notify_admin_access_request(self, access_request) -> bool:
        """
        Beri tahu operator bahwa ada permintaan akses API baru.

        Dipanggil dari endpoint publik, jadi kegagalannya tidak menular ke
        respons: permintaannya sudah tersimpan apa pun yang terjadi di sini.
        """
        context = {"request_obj": access_request}
        return self._send_as_engine(
            subject=f"[ragai] Permintaan akses API dari {access_request.email}",
            message=render_to_string("emails/access_request_admin.txt", context),
            recipients=self.admin_emails,
            html_message=render_to_string("emails/access_request_admin.html", context),
        )

    def send_api_key(self, recipient: str, api_key: str, name: str = "",
                     label: str = "", rate: str = "", base_url: str = "") -> bool:
        """
        Kirim kunci API kepada pemohon.

        Nilai asli kunci hanya ada di sini dan di terminal operator, jadi
        kegagalan pengiriman dikembalikan ke pemanggil, bukan ditelan.
        """
        context = {
            "name": name or recipient,
            "api_key": api_key,
            "label": label,
            "rate": rate or getattr(settings, "INTELLIGENCE_RATE_LIMIT_LABEL",
                                    "60 requests per minute"),
            "base_url": (base_url
                         or getattr(settings, "PUBLIC_API_BASE_URL",
                                    "https://ragai.twenti.studio")).rstrip("/"),
        }
        return self._send_as_engine(
            subject="Your ragai API key",
            message=render_to_string("emails/api_key_delivery.txt", context),
            recipients=[recipient],
            html_message=render_to_string("emails/api_key_delivery.html", context),
        )


# Singleton instance
email_service = EmailNotificationService()