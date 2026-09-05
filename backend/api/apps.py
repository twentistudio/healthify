from django.apps import AppConfig


class ApiConfig(AppConfig):
    """Aplikasi Healthify: verifikasi klaim, dispute, dan admin."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        """
        Daftarkan Healthify sebagai penyedia bagi engine ragai.

        Engine berdiri sebagai produk tersendiri dan tidak mengimpor apa pun
        dari aplikasi ini. Yang dibutuhkannya disebutkan lewat nama peran, dan
        di sinilah peran itu dipenuhi: model tempat menyimpan, serta layanan
        terjemahan dan embedding milik Healthify.
        """
        from ragai import runtime

        from .models import (
            ClaimSource,
            ConsultationSummary,
            ConversationMessage,
            ConversationSession,
            JournalArticle,
            Source,
        )

        runtime.configure(
            models={
                "JournalArticle": JournalArticle,
                "Source": Source,
                "ClaimSource": ClaimSource,
                "ConversationSession": ConversationSession,
                "ConversationMessage": ConversationMessage,
                "ConsultationSummary": ConsultationSummary,
            },
            services={
                # Diambil malas lewat fungsi pembungkus: mengimpor `views` dan
                # `ai_adapter` di sini akan berjalan sebelum aplikasi siap.
                "translate": _translate,
                "embed_article": _embed_article,
                "training_scripts_dir": _training_scripts_dir,
                "training_modules_available": _training_modules_available,
            },
        )


def _translate(text, target_lang):
    from .views import translate_text

    return translate_text(text, target_lang)


def _embed_article(article):
    from .views import embed_journal_article

    return embed_journal_article(article)


def _training_scripts_dir():
    from .ai_adapter import TRAINING_SCRIPTS_DIR

    return TRAINING_SCRIPTS_DIR


def _training_modules_available():
    from .ai_adapter import training_modules_available

    return training_modules_available()
