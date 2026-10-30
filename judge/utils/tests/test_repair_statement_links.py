import io
import os
import shutil
import tempfile
from contextlib import redirect_stdout

from django.core.management import call_command
from django.test import TestCase

from judge.models import Problem
from judge.models.tests.util import create_problem
from judge.models.problem_alias import ProblemAlias


def alias(problem, name):
    return ProblemAlias.objects.create(problem=problem, name=name)


class RepairStatementLinksTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='repair-links-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.media_root = os.path.join(self.tmp, 'media', 'problems')
        os.makedirs(self.media_root)

    def run_command(self, *args, **options):
        from django.test import override_settings
        settings = override_settings(DMOJ_PROBLEM_MEDIA_ROOT=self.media_root)
        settings.enable()
        self.addCleanup(settings.disable)
        out = io.StringIO()
        with redirect_stdout(out):
            call_command('repair_statement_links', *args, **options)
        return out.getvalue()

    def test_rewrites_slug_references_to_uuid_paths(self):
        problem = create_problem(code='79c6d016-5cf7-4cce-a1ed-50e22c20177a')
        problem.description = (
            'Leé [la plantilla](https://polijuez.ar/media/problems/ipshaskanagrama/template.hs).\n'
            'Y [el pdf](/media/problems/ipshaskanagrama/statement.pdf).\n'
        )
        problem.save()
        alias(problem, 'ipshaskanagrama')

        self.run_command('--apply')

        problem.refresh_from_db()
        self.assertEqual(
            problem.description,
            'Leé [la plantilla](/media/problems/79c6d016-5cf7-4cce-a1ed-50e22c20177a/template.hs).\n'
            'Y [el pdf](/media/problems/79c6d016-5cf7-4cce-a1ed-50e22c20177a/statement.pdf).\n',
        )

    def test_is_idempotent(self):
        problem = create_problem(code='79c6d016-5cf7-4cce-a1ed-50e22c20177a')
        problem.description = 'Leé [esto](/media/problems/ipshaskanagrama/x.hs).\n'
        problem.save()
        alias(problem, 'ipshaskanagrama')

        self.run_command('--apply')
        self.run_command('--apply')

        problem.refresh_from_db()
        self.assertEqual(problem.description,
                         'Leé [esto](/media/problems/79c6d016-5cf7-4cce-a1ed-50e22c20177a/x.hs).\n')

    def test_dry_run_does_not_touch_the_database(self):
        problem = create_problem(code='79c6d016-5cf7-4cce-a1ed-50e22c20177a')
        problem.description = 'Leé [esto](/media/problems/ipshaskanagrama/x.hs).\n'
        problem.save()
        alias(problem, 'ipshaskanagrama')

        output = self.run_command()

        problem.refresh_from_db()
        self.assertEqual(problem.description,
                         'Leé [esto](/media/problems/ipshaskanagrama/x.hs).\n')
        self.assertIn('Dry-run', output)

    def test_unmapped_references_are_reported_and_left_alone(self):
        problem = create_problem(code='79c6d016-5cf7-4cce-a1ed-50e22c20177a')
        problem.description = 'Un [misterio](/media/problems/desaparecido/x.zip).\n'
        problem.save()
        alias(problem, 'ipshaskanagrama')

        output = self.run_command('--apply')

        problem.refresh_from_db()
        self.assertEqual(problem.description,
                         'Un [misterio](/media/problems/desaparecido/x.zip).\n')
        self.assertIn('desaparecido', output)

    def test_renames_leftover_alias_media_directories(self):
        problem = create_problem(code='2ee28616-6d4f-4595-a4e6-045d1dfe67a3')
        alias(problem, 'aprendiendo')
        source = os.path.join(self.media_root, 'aprendiendo')
        os.makedirs(source)
        with open(os.path.join(source, 'statement.pdf'), 'wb') as handle:
            handle.write(b'%PDF-1.4\n')

        self.run_command('--apply', '--rename-media')

        self.assertFalse(os.path.exists(source))
        target = os.path.join(self.media_root, '2ee28616-6d4f-4595-a4e6-045d1dfe67a3')
        self.assertEqual(os.listdir(target), ['statement.pdf'])
