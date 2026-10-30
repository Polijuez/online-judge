"""Reparar los enlaces de enunciados que quedaron apuntando a slugs viejos.

La migración a ids UUID renombró los directorios de medios pero no tocó las
``description``: los problemas publicados antes del corte siguen enlazando
``/media/problems/<slug>/...``, que hoy es un 404. Este comando reescribe esas
referencias usando la tabla de alias (el slug viejo es un alias del problema),
y de paso normaliza las URLs absolutas con dominio a rutas relativas al sitio.

Es idempotente: correrlo de nuevo no encuentra nada. Sin ``--apply`` solo
reporta el plan.
"""
import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from judge.models import Problem, ProblemAlias

# Cualquier /media/problems/<segmento>/... sea relativo al sitio o con dominio.
_MEDIA_REF_RE = re.compile(r'(?:https?://[^/\s"\')]*)?(/media/problems/)([^/\s"\')]+)/')

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _is_uuid(value):
    return bool(_UUID_RE.match(value or ''))


class Command(BaseCommand):
    help = ('rewrite legacy /media/problems/<slug>/ references in problem statements '
            'to the UUID media path (dry-run by default), using the alias table')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='rewrite the descriptions and rename leftover media dirs; '
                                 'without it only the plan is reported')
        parser.add_argument('--rename-media', action='store_true',
                            help='also rename media directories that still use an alias '
                                 'instead of the problem UUID')

    def handle(self, *args, **options):
        alias_to_problem = {}  # alias (str) -> Problem instance
        for alias in ProblemAlias.objects.select_related('problem'):
            alias_to_problem[alias.name] = alias.problem

        problems = list(Problem.objects.order_by('id'))
        rewritten = []
        unmapped = []
        for problem in problems:
            description = problem.description or ''
            found_unmapped = set()

            def replace(match):
                base, segment = match.group(1), match.group(2)
                if _is_uuid(segment):
                    return match.group(0)
                if segment == problem.code:
                    return match.group(0)
                target = alias_to_problem.get(segment)
                if target is None:
                    found_unmapped.add(segment)
                    return match.group(0)
                return base + target.code + '/'

            new_description = _MEDIA_REF_RE.sub(replace, description)
            if found_unmapped:
                unmapped.append((problem, sorted(found_unmapped)))
            if new_description != description:
                rewritten.append((problem, description, new_description))

        renames = []
        if options['rename_media']:
            media_root = getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
            if not media_root:
                raise CommandError('DMOJ_PROBLEM_MEDIA_ROOT is not configured')
            for alias, problem in sorted(alias_to_problem.items()):
                source = os.path.join(media_root, alias)
                target = os.path.join(media_root, problem.code)
                if os.path.isdir(source) and not os.path.exists(target):
                    renames.append((alias, problem.code, source, target))

        self.stdout.write('Problemas: %d' % len(problems))
        self.stdout.write('Descripciones con enlaces a reescribir: %d' % len(rewritten))
        for problem, _, new_description in rewritten:
            count = len(_MEDIA_REF_RE.findall(new_description))
            self.stdout.write('  %s  %s  (%d enlace/s)' % (problem.code, problem.name, count))
        if unmapped:
            self.stdout.write('')
            self.stdout.write('Referencias sin alias que las resuelva (%d):' % len(unmapped))
            for problem, segments in unmapped:
                for segment in segments:
                    self.stdout.write('  %s  %s  (queda como está)' % (problem.code, segment))
        if renames:
            self.stdout.write('')
            self.stdout.write('Directorios de medios a renombrar (%d):' % len(renames))
            for alias, code, source, target in renames:
                self.stdout.write('  %s -> %s' % (alias, code))
        self.stdout.write('')

        if not options['apply']:
            self.stdout.write('Dry-run: no se reescribió nada ni se renombró nada; '
                              'volvé a correr con --apply para aplicar.')
            return

        for problem, _, new_description in rewritten:
            problem.description = new_description
            problem.save(update_fields=['description'])
        for alias, code, source, target in renames:
            os.rename(source, target)
            self.stdout.write('renombrado  %s -> %s' % (alias, code))

        self.stdout.write('Listo: %d descripción/es reescrita(s), %d directorio(s) renombrado(s).' % (
            len(rewritten), len(renames),
        ))
