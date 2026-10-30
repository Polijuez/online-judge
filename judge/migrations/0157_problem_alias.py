# La tabla de alias la borra 0160: esta migración queda como historia. El validador se define acá en
# vez de importarlo de `judge.models.problem_alias` (módulo eliminado) para que el archivo siga siendo
# importable por `makemigrations`/`migrate`.
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion

ALIAS_VALIDATOR = django.core.validators.RegexValidator(
    r'^[A-Za-z0-9_-]{1,40}$', 'Aliases must match ^[a-z0-9_-]{1,40}$.',
)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0156_problemdata_evaluator_problemdata_signature_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemAlias',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Human-readable name that redirects to the problem URL.',
                                          max_length=40, unique=True,
                                          validators=[ALIAS_VALIDATOR],
                                          verbose_name='alias')),
                ('problem', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='aliases', to='judge.problem',
                                              verbose_name='problem')),
            ],
            options={
                'verbose_name': 'problem alias',
                'verbose_name_plural': 'problem aliases',
                'ordering': ('name',),
            },
        ),
    ]
