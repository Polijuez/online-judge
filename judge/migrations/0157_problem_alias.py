from django.db import migrations, models
import django.db.models.deletion

import judge.models.problem_alias


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
                                          validators=[judge.models.problem_alias.ALIAS_VALIDATOR],
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
