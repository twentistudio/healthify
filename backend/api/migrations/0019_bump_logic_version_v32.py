# Menaikkan default logic_version ke v3.2.
#
# Hanya AlterField pada default; tidak ada data yang disentuh. Hasil lama tetap
# tersimpan dan akan diverifikasi ulang oleh mesin terkini saat diminta.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0018_bump_logic_version_v31'),
    ]

    operations = [
        migrations.AlterField(
            model_name='verificationresult',
            name='logic_version',
            field=models.CharField(
                default='v3.2',
                help_text='Versi mesin yang menghasilkan penilaian ini. '
                          'Hasil dari versi lama tidak disajikan dari cache.',
                max_length=32,
                null=True,
            ),
        ),
    ]
