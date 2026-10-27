import io
import os
import shutil
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from judge.models.tests.util import create_problem
from judge.utils import problem_migration
from judge.utils.problem_migration import MigrationError, build_plan

PROBLEM_A = '11111111-1111-4111-8111-111111111111'
PROBLEM_B = '22222222-2222-4222-8222-222222222222'
PROBLEM_DB_ONLY = '33333333-3333-4333-8333-333333333333'


def tree(root):
    found = {}
    for current, _, files in os.walk(root):
        for name in files:
            path = os.path.join(current, name)
            with open(path, 'rb') as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


class ProblemMigrationTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='problem-migration-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = os.path.join(self.tmp, 'problems')
        self.media_root = os.path.join(self.tmp, 'media', 'problems')
        os.makedirs(self.data_root)
        os.makedirs(self.media_root)
        self.manifest = os.path.join(self.tmp, 'problem-tree-manifest.tsv')
        self.settings = override_settings(
            DMOJ_PROBLEM_DATA_ROOT=self.data_root,
            DMOJ_PROBLEM_MEDIA_ROOT=self.media_root,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    # -- helpers ---------------------------------------------------------

    def make_problem(self, code, alias, title=None):
        problem = create_problem(code=code, name=title or alias)
        problem.add_alias(alias)
        return problem

    def make_tree(self, root, relative, files=None):
        path = os.path.join(root, relative)
        os.makedirs(path, exist_ok=True)
        for name, content in (files or {'init.yml': b'test_cases: []\n'}).items():
            with open(os.path.join(path, name), 'wb') as handle:
                handle.write(content)
        return path

    def stage_old_tree(self):
        """Dos problemas con árbol, uno con medios, uno solo en la base y un directorio huérfano."""
        self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        self.make_problem(PROBLEM_B, 'suma', 'Suma')
        self.make_problem(PROBLEM_DB_ONLY, 'aprendiendo', 'Aprendiendo')
        self.make_tree(self.data_root, 'problemset/camellos',
                       {'init.yml': b'archive: data.zip\n', 'data.zip': b'zip'})
        self.make_tree(self.media_root, 'camellos', {'figura.png': b'png'})
        self.make_tree(self.data_root, 'problemset/suma')
        self.make_tree(self.data_root, 'problemset/pompeya')
        # Lo que el ticket 14 limpia: no se toca ni se mueve.
        self.make_tree(self.data_root, 'problemset/.git', {'HEAD': b'ref: refs/heads/main\n'})
        with open(os.path.join(self.data_root, 'problemset', 'judge.yml'), 'w') as handle:
            handle.write('name: problemset\n')
        with open(os.path.join(self.data_root, 'judge.yml'), 'w') as handle:
            handle.write('name: root\n')

    def run_command(self, *args, **options):
        out = io.StringIO()
        options.setdefault('manifest', self.manifest)
        self.last_output = out
        call_command('migrate_problem_ids', *args, stdout=out, **options)
        return out.getvalue()

    # -- dry-run ---------------------------------------------------------

    def test_dry_run_lists_every_problem_and_the_divergences_without_touching_anything(self):
        self.stage_old_tree()
        before = tree(self.data_root), tree(self.media_root)

        out = self.run_command()

        self.assertIn('Problemas: 3', out)
        self.assertIn(PROBLEM_A, out)
        self.assertIn('mover datos problemset/camellos -> %s' % PROBLEM_A, out)
        self.assertIn('medios camellos -> %s' % PROBLEM_A, out)
        self.assertIn("alias 'camellos'", out)
        self.assertIn('mover datos problemset/suma -> %s' % PROBLEM_B, out)
        self.assertIn('Cuarentena (2):', out)
        self.assertIn('directorio de datos  problemset/pompeya  '
                      "no hay problema con el alias 'pompeya'", out)
        self.assertIn('problema  %s  sin directorio de datos' % PROBLEM_DB_ONLY, out)
        self.assertIn('Dry-run: no se movió nada', out)

        self.assertEqual((tree(self.data_root), tree(self.media_root)), before)
        self.assertFalse(os.path.exists(self.manifest))

    def test_dry_run_without_media_root_still_plans_data(self):
        self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        self.make_tree(self.data_root, 'problemset/camellos')

        with override_settings(DMOJ_PROBLEM_MEDIA_ROOT=None):
            out = self.run_command()

        self.assertIn('Raíz de medios: sin configurar', out)
        self.assertIn('mover datos problemset/camellos -> %s' % PROBLEM_A, out)

    # -- apply -----------------------------------------------------------

    def test_apply_moves_data_and_media_and_writes_the_manifest(self):
        self.stage_old_tree()

        out = self.run_command(apply=True)

        self.assertIn('movido  problemset/camellos -> %s' % PROBLEM_A, out)
        self.assertIn('movido  medios camellos -> %s' % PROBLEM_A, out)
        self.assertIn('Listo: 2 problema(s) movido(s), 2 en cuarentena.', out)

        self.assertFalse(os.path.exists(os.path.join(self.data_root, 'problemset', 'camellos')))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, 'camellos')))
        self.assertEqual(tree(os.path.join(self.data_root, PROBLEM_A)),
                         {'init.yml': b'archive: data.zip\n', 'data.zip': b'zip'})
        self.assertEqual(tree(os.path.join(self.media_root, PROBLEM_A)), {'figura.png': b'png'})
        self.assertEqual(tree(os.path.join(self.data_root, PROBLEM_B)), {'init.yml': b'test_cases: []\n'})
        self.assertFalse(os.path.exists(os.path.join(self.media_root, PROBLEM_B)))

        # Las divergencias quedan donde estaban.
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, 'problemset', 'pompeya')))
        self.assertFalse(os.path.exists(os.path.join(self.data_root, PROBLEM_DB_ONLY)))

        # Y lo que es del ticket 14, intacto.
        self.assertEqual(tree(os.path.join(self.data_root, 'problemset', '.git')),
                         {'HEAD': b'ref: refs/heads/main\n'})
        self.assertTrue(os.path.exists(os.path.join(self.data_root, 'problemset', 'judge.yml')))
        self.assertTrue(os.path.exists(os.path.join(self.data_root, 'judge.yml')))

        with open(self.manifest, encoding='utf-8') as manifest:
            content = manifest.read()
        self.assertIn('# problemset\tslug\tproblem_id\talias\tdata_from\tdata_to\t'
                      'media_from\tmedia_to', content)
        self.assertIn('problemset\tcamellos\t%s\tcamellos\tproblemset/camellos\t%s\t'
                      'camellos\t%s' % (PROBLEM_A, PROBLEM_A, PROBLEM_A), content)
        self.assertIn('problemset\tsuma\t%s\tsuma\tproblemset/suma\t%s\t\t' % (PROBLEM_B, PROBLEM_B),
                      content)
        self.assertIn("# cuarentena\tdata-dir\tproblemset/pompeya\t"
                      "no hay problema con el alias 'pompeya'", content)
        self.assertIn('# cuarentena\tproblem\t%s\tsin directorio de datos' % PROBLEM_DB_ONLY,
                      content)

    def test_second_run_is_a_noop(self):
        self.stage_old_tree()
        self.run_command(apply=True)
        with open(self.manifest, 'rb') as manifest:
            first_manifest = manifest.read()
        first_stat = os.stat(self.manifest)
        before = tree(self.data_root), tree(self.media_root)

        out = self.run_command(apply=True)

        self.assertIn('Nada que mover: el árbol ya está migrado.', out)
        self.assertNotIn('movido', out.split('Nada que mover')[0])
        self.assertEqual((tree(self.data_root), tree(self.media_root)), before)
        with open(self.manifest, 'rb') as manifest:
            self.assertEqual(manifest.read(), first_manifest)
        self.assertEqual(os.stat(self.manifest).st_mtime_ns, first_stat.st_mtime_ns)

    def test_interrupted_run_resumes_with_the_original_mapping(self):
        self.stage_old_tree()
        real_rename = problem_migration._rename
        calls = []

        def flaky(move):
            calls.append(move)
            if len(calls) == 3:
                raise MigrationError('interrumpido a propósito')
            return real_rename(move)

        with mock.patch.object(problem_migration, '_rename', side_effect=flaky):
            with self.assertRaises(CommandError):
                self.run_command(apply=True)

        # El primer problema quedó movido y su mapeo quedó escrito.
        with open(self.manifest, encoding='utf-8') as manifest:
            interrupted = manifest.read()
        self.assertIn('problemset\tcamellos\t%s' % PROBLEM_A, interrupted)
        self.assertIn('problemset\tsuma\t%s' % PROBLEM_B, interrupted)

        out = self.run_command(apply=True)

        self.assertIn('Listo: 1 problema(s) movido(s)', out)
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, PROBLEM_A)))
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, PROBLEM_B)))
        self.assertFalse(os.path.exists(os.path.join(self.data_root, 'problemset', 'suma')))
        with open(self.manifest, encoding='utf-8') as manifest:
            resumed = manifest.read()
        self.assertIn('problemset\tsuma\t%s\tsuma\tproblemset/suma\t%s' % (PROBLEM_B, PROBLEM_B),
                      resumed)

    def test_resume_after_interruption_between_data_and_media(self):
        self.stage_old_tree()
        real_rename = problem_migration._rename
        calls = []

        def flaky(move):
            calls.append(move)
            if len(calls) == 2:
                raise MigrationError('interrumpido a propósito')
            return real_rename(move)

        with mock.patch.object(problem_migration, '_rename', side_effect=flaky):
            with self.assertRaises(CommandError):
                self.run_command(apply=True)

        # El árbol de datos del primer problema se movió; sus medios no.
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, PROBLEM_A)))
        self.assertFalse(os.path.exists(os.path.join(self.data_root, 'problemset', 'camellos')))
        self.assertTrue(os.path.isdir(os.path.join(self.media_root, 'camellos')))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, PROBLEM_A)))

        out = self.run_command(apply=True)

        self.assertIn('datos ya migrados; mover medios camellos -> %s' % PROBLEM_A, out)
        self.assertIn('Listo: 2 problema(s) movido(s)', out)
        self.assertEqual(tree(os.path.join(self.media_root, PROBLEM_A)), {'figura.png': b'png'})
        self.assertFalse(os.path.exists(os.path.join(self.media_root, 'camellos')))
        with open(self.manifest, encoding='utf-8') as manifest:
            resumed = manifest.read()
        self.assertIn('problemset\tcamellos\t%s\tcamellos\tproblemset/camellos\t%s\t'
                      'camellos\t%s' % (PROBLEM_A, PROBLEM_A, PROBLEM_A), resumed)

    def test_media_of_a_problem_without_data_is_quarantined(self):
        self.make_problem(PROBLEM_DB_ONLY, 'aprendiendo', 'Aprendiendo')
        self.make_tree(self.media_root, 'aprendiendo', {'figura.png': b'png'})

        out = self.run_command(apply=True)

        self.assertIn('el problema %s no tiene directorio de datos' % PROBLEM_DB_ONLY, out)
        self.assertTrue(os.path.isdir(os.path.join(self.media_root, 'aprendiendo')))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, PROBLEM_DB_ONLY)))

    # -- refusal ---------------------------------------------------------

    def test_refuses_two_sources_claiming_the_same_destination(self):
        problem = self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        problem.add_alias('horse')
        self.make_tree(self.data_root, 'problemset/camellos')
        self.make_tree(self.data_root, 'otro/horse')

        with self.assertRaises(CommandError) as caught:
            self.run_command(apply=True)

        self.assertIn('se rehúsa a migrar: hay 1 conflicto(s) de destino', str(caught.exception))
        self.assertIn('dos orígenes pelean por el destino %s' % PROBLEM_A,
                      self.last_output.getvalue())
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, 'problemset', 'camellos')))
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, 'otro', 'horse')))
        self.assertFalse(os.path.exists(os.path.join(self.data_root, PROBLEM_A)))
        self.assertFalse(os.path.exists(self.manifest))

    def test_refuses_when_the_destination_already_exists(self):
        self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        self.make_tree(self.data_root, 'problemset/camellos')
        self.make_tree(self.data_root, PROBLEM_A)

        with self.assertRaises(CommandError) as caught:
            self.run_command(apply=True)

        self.assertIn('se rehúsa a migrar: hay 1 conflicto(s) de destino', str(caught.exception))
        self.assertIn('el destino %s ya existe y el origen problemset/camellos también' % PROBLEM_A,
                      self.last_output.getvalue())
        self.assertTrue(os.path.isdir(os.path.join(self.data_root, 'problemset', 'camellos')))
        self.assertFalse(os.path.exists(self.manifest))

    def test_refuses_two_media_sources_claiming_the_same_destination(self):
        problem = self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        problem.add_alias('horse')
        self.make_tree(self.data_root, PROBLEM_A)
        self.make_tree(self.media_root, 'camellos', {'figura.png': b'png'})
        self.make_tree(self.media_root, 'horse', {'otra.png': b'png'})

        with self.assertRaises(CommandError) as caught:
            self.run_command(apply=True)

        self.assertIn('se rehúsa a migrar: hay 1 conflicto(s) de destino', str(caught.exception))
        self.assertIn('dos directorios de medios pelean por el destino %s' % PROBLEM_A,
                      self.last_output.getvalue())
        self.assertTrue(os.path.isdir(os.path.join(self.media_root, 'camellos')))
        self.assertTrue(os.path.isdir(os.path.join(self.media_root, 'horse')))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, PROBLEM_A)))
        self.assertFalse(os.path.exists(self.manifest))

    def test_plan_is_empty_when_the_tree_is_already_migrated(self):
        self.make_problem(PROBLEM_A, 'camellos', 'Camellos')
        self.make_tree(self.data_root, PROBLEM_A)

        plan = build_plan(self.data_root, self.media_root)

        self.assertEqual(plan.pending, [])
        self.assertEqual([item.state for item in plan.problems], ['done'])
        self.assertEqual(plan.quarantine, [])

    def test_missing_data_root_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command(data_root=os.path.join(self.tmp, 'nope'))
