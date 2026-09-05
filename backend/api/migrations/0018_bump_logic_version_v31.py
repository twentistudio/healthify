# Menaikkan default logic_version ke v3.1.
#
# Hanya AlterField pada default; tidak ada data yang disentuh. Hasil verifikasi
# lama tetap tersimpan apa adanya dan akan diverifikasi ulang oleh mesin
# terkini saat diminta, karena cache mengabaikan versi yang lebih lama.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0017_alter_verificationresult_logic_version'),
    ]

    operations = [
        migrations.AlterField(
            model_name='verificationresult',
            name='logic_version',
            field=models.CharField(
                default='v3.1',
                help_text='Versi mesin yang menghasilkan penilaian ini. '
                          'Hasil dari versi lama tidak disajikan dari cache.',
                max_length=32,
                null=True,
            ),
        ),
    ]
