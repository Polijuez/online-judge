from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from judge.utils.problem_migration import (
    QUARANTINE_DATA_DIR, QUARANTINE_MEDIA_DIR, STATE_DONE, STATE_MISSING, STATE_MOVE,
    MigrationError, apply_plan, build_plan, default_manifest_path,
)

QUARANTINE_LABELS = {
    QUARANTINE_DATA_DIR: 'directorio de datos',
    QUARANTINE_MEDIA_DIR: 'directorio de medios',
}


class Command(BaseCommand):
    help = ('move the existing problem tree to UUID directory names (dry-run by default), '
            'reporting divergences and writing the mapping manifest')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='move the directories; without it only the plan is reported')
        parser.add_argument('--data-root', help='overrides DMOJ_PROBLEM_DATA_ROOT')
        parser.add_argument('--media-root', help='overrides DMOJ_PROBLEM_MEDIA_ROOT')
        parser.add_argument('--manifest', help='where to write the mapping manifest (default: '
                                              '$DMOJ_PROBLEM_TREE_MANIFEST or '
                                              'problem-tree-manifest.tsv in BASE_DIR)')

    def handle(self, *args, **options):
        data_root = options['data_root'] or getattr(settings, 'DMOJ_PROBLEM_DATA_ROOT', None)
        if not data_root:
            raise CommandError('DMOJ_PROBLEM_DATA_ROOT is not configured; pass --data-root')
        media_root = options['media_root'] or getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
        manifest = options['manifest'] or default_manifest_path()

        try:
            plan = build_plan(data_root, media_root)
        except MigrationError as error:
            raise CommandError(str(error))

        self._report(plan, manifest)

        if plan.conflicts:
            raise CommandError('se rehúsa a migrar: hay %d conflicto(s) de destino' % len(plan.conflicts))

        if not options['apply']:
            self.stdout.write('Dry-run: no se movió nada ni se reescribió la base; '
                              'volvé a correr con --apply para aplicar.')
            return

        if not plan.pending and not plan.rewrites:
            self.stdout.write('Nada que mover: el árbol ya está migrado.')
            self.stdout.write('Nada que reescribir: las rutas de ProblemData ya apuntan a los ids.')
            if plan.file_skips:
                self.stdout.write('Quedan %d archivo(s) sin resolver (ver arriba).' % len(plan.file_skips))
            self.stdout.write('Manifiesto sin cambios: %s' % manifest)
            return

        try:
            result = apply_plan(plan, manifest, on_moved=self._moved)
        except (MigrationError, OSError) as error:
            raise CommandError('%s (el manifiesto y lo ya movido quedan; volvé a correr para reanudar)'
                               % error)

        if plan.pending:
            self.stdout.write('Manifiesto: %s' % manifest)
        self.stdout.write('Listo: %d problema(s) movido(s), %d ruta(s) reescrita(s), %d en cuarentena.' % (
            result.moved, result.rewritten, len(plan.quarantine),
        ))

    def _report(self, plan, manifest):
        self.stdout.write('Raíz de datos: %s' % plan.data_root)
        if plan.media_root:
            self.stdout.write('Raíz de medios: %s' % plan.media_root)
        else:
            self.stdout.write('Raíz de medios: sin configurar; no se migran medios')
        self.stdout.write('Manifiesto: %s' % manifest)
        self.stdout.write('')
        self.stdout.write('Problemas: %d' % len(plan.problems))

        for item in plan.problems:
            self.stdout.write('  %s  %s' % (item.problem_id, item.title))
            if item.state == STATE_MOVE:
                detail = 'mover datos %s/%s -> %s' % (
                    item.data.problemset, item.data.slug, item.problem_id,
                )
                if item.media is not None:
                    detail += '; medios %s -> %s' % (item.media.slug, item.problem_id)
                detail += '; alias %r' % item.alias
                self.stdout.write('      %s' % detail)
            elif item.state == STATE_DONE and item.media is not None:
                self.stdout.write('      datos ya migrados; mover medios %s -> %s' % (
                    item.media.slug, item.problem_id,
                ))
            elif item.state == STATE_DONE:
                self.stdout.write('      ya migrado: %s/' % item.problem_id)
            elif item.state == STATE_MISSING:
                self.stdout.write('      cuarentena: sin directorio de datos')

        self.stdout.write('')
        if plan.rewrites:
            self.stdout.write('Reescrituras de ProblemData (%d):' % len(plan.rewrites))
            for rewrite in plan.rewrites:
                self.stdout.write('  %s  %s  %s -> %s' % (
                    rewrite.problem_id, rewrite.field, rewrite.before, rewrite.after,
                ))
        else:
            self.stdout.write('Reescrituras de ProblemData: ninguna')

        if plan.file_skips:
            self.stdout.write('Archivos que no se reescriben (%d):' % len(plan.file_skips))
            for skip in plan.file_skips:
                self.stdout.write('  %s  %s  %s  %s' % (
                    skip.problem_id, skip.field, skip.path, skip.detail,
                ))

        self.stdout.write('')
        if plan.quarantine:
            self.stdout.write('Cuarentena (%d):' % len(plan.quarantine))
            for item in plan.quarantine:
                label = QUARANTINE_LABELS.get(item.kind, 'problema')
                self.stdout.write('  %s  %s  %s' % (label, item.path, item.detail))
        else:
            self.stdout.write('Cuarentena: ninguna')

        if plan.conflicts:
            self.stdout.write('')
            self.stdout.write('Conflictos (%d):' % len(plan.conflicts))
            for conflict in plan.conflicts:
                self.stdout.write('  %s' % conflict)
        self.stdout.write('')

    def _moved(self, item):
        if item.data is not None:
            self.stdout.write('  movido  %s/%s -> %s' % (
                item.data.problemset, item.data.slug, item.problem_id,
            ))
        if item.media is not None:
            self.stdout.write('  movido  medios %s -> %s' % (item.media.slug, item.problem_id))
