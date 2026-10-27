# El id del problema pasa a ser un UUID: se ensancha `Problem.code`, se valida como UUID y los
# problemas existentes reciben un uuid4. Antes de reescribir el código, cada problema migrado recibe
# como alias su código viejo (el slug previo), así las URLs viejas siguen resolviendo; la tabla de
# alias se crea en la migración 0157, de la que esta depende. El mapeo (id viejo -> uuid) se escribe
# en el archivo que indique DMOJ_PROBLEM_UUID_REPORT, o en problem-uuid-mapping.txt dentro de BASE_DIR.
# La migración es idempotente: los problemas que ya tienen un id con forma de UUID se saltean, y un
# segundo intento no reescribe el reporte.
import os
import re
import uuid

import django.core.validators
from django.conf import settings
from django.db import migrations, models

import judge.models.problem

PROBLEM_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
# Mismo formato que `judge.models.problem_alias.ALIAS_REGEX`, para no importar modelos en la migración.
ALIAS_RE = re.compile(r'^[a-z0-9_-]{1,40}$')


def report_path():
    return os.environ.get('DMOJ_PROBLEM_UUID_REPORT',
                          os.path.join(settings.BASE_DIR, 'problem-uuid-mapping.txt'))


def assign_uuids(apps, schema_editor):
    Problem = apps.get_model('judge', 'Problem')
    ProblemAlias = apps.get_model('judge', 'ProblemAlias')
    used = set(Problem.objects.values_list('code', flat=True))
    mapping = []

    for problem in Problem.objects.order_by('id').only('id', 'code'):
        if PROBLEM_ID_RE.match(problem.code):
            continue

        old_code = problem.code
        new_code = str(uuid.uuid4())
        while new_code in used:
            new_code = str(uuid.uuid4())
        used.add(new_code)

        alias = old_code.lower()
        # Los códigos viejos eran `[a-z0-9]{1,20}`, así que siempre entran en el formato de alias; el
        # guardado evita que una base tocada a mano haga fallar la migración entera.
        if ALIAS_RE.match(alias) and not ProblemAlias.objects.filter(name=alias).exists():
            ProblemAlias.objects.create(problem_id=problem.pk, name=alias)

        mapping.append((old_code, new_code))
        problem.code = new_code
        problem.save(update_fields=['code'])

    if mapping:
        with open(report_path(), 'w', encoding='utf-8') as report:
            for old_code, new_code in mapping:
                report.write('%s -> %s\n' % (old_code, new_code))


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0157_problem_alias'),
    ]

    operations = [
        migrations.AlterField(
            model_name='comment',
            name='page',
            field=models.CharField(db_index=True, max_length=40, validators=[django.core.validators.RegexValidator('^[pcs]:[a-z0-9-]+$|^b:\\d+$', 'Page code must be ^[pcs]:[a-z0-9-]+$|^b:\\d+$')], verbose_name='associated page'),
        ),
        migrations.AlterField(
            model_name='commentlock',
            name='page',
            field=models.CharField(db_index=True, max_length=40, validators=[django.core.validators.RegexValidator('^[pcs]:[a-z0-9-]+$|^b:\\d+$', 'Page code must be ^[pcs]:[a-z0-9-]+$|^b:\\d+$')], verbose_name='associated page'),
        ),
        migrations.AlterField(
            model_name='problem',
            name='code',
            field=models.CharField(default=judge.models.problem.generate_problem_code, help_text='The UUID of the problem, used in the URL after /problem/', max_length=36, unique=True, validators=[django.core.validators.RegexValidator('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'Problem id must be a UUID.')], verbose_name='problem id'),
        ),
        migrations.RunPython(assign_uuids, migrations.RunPython.noop),
    ]
