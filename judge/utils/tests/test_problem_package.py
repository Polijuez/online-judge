import io
import os
import shutil
import tempfile
import zipfile
from unittest import mock

from django.test import TestCase, override_settings

from judge.models import Language, LanguageLimit, Problem, ProblemData, ProblemTestCase
from judge.models.tests.util import create_user
from judge.utils import problem_package
from judge.utils.problem_package import PackageError, publish_package

CASE_IN = b'1 2\n'
CASE_OUT = b'3\n'

INIT_YML = b'archive: data.zip\ntest_cases:\n- points: 100\n  in: 1.in\n  out: 1.out\n'
INIT_BATCHED = (
    b'archive: data.zip\n'
    b'test_cases:\n'
    b'- points: 100\n'
    b'  batched:\n'
    b'  - {in: 1.in, out: 1.out}\n'
)

MANIFEST = {
    'schema': 1,
    'title': 'Suma de dos',
    'aliases': ['suma-de-dos', 'suma'],
    'limits': {
        '.cpp': {'tl': 1.0, 'ml': 262144},
        '.py': {'tl': 2.5, 'ml': 131072},
    },
    'types': ['math'],
    'group': 'basico',
    'visibility': 'public',
}


def yaml_manifest(overrides=None):
    data = dict(MANIFEST)
    if overrides:
        data.update(overrides)
    return data


def yaml_dump(data):
    import yaml
    return yaml.safe_dump(data).encode('utf-8')


def case_zip(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        files = files if files is not None else {'1.in': CASE_IN, '1.out': CASE_OUT}
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def package(manifest=None, init=INIT_YML, statement='# Enunciado\n', cases=None, extra=None, media=None,
            omit=(), archive_name='data.zip'):
    """Arma un zip de paquete; ``omit`` saca archivos por nombre."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        if 'problem.yml' not in omit:
            archive.writestr('problem.yml', yaml_dump(manifest or yaml_manifest()))
        if 'init.yml' not in omit:
            archive.writestr('init.yml', init)
        if 'statement.md' not in omit:
            archive.writestr('statement.md', statement)
        if archive_name not in omit:
            archive.writestr(archive_name, case_zip(cases))
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
        for name, content in (media or {}).items():
            archive.writestr('media/' + name, content)
    return buffer.getvalue()


def tree(root):
    if not os.path.isdir(root):
        return {}
    result = {}
    for current, _, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(current, filename)
            result[os.path.relpath(path, root)] = open(path, 'rb').read()
    return result


class ProblemPackageTestCase(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='problem-package-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = os.path.join(self.tmp, 'data')
        self.media_root = os.path.join(self.tmp, 'media', 'problems')
        self.settings = override_settings(
            DMOJ_PROBLEM_DATA_ROOT=self.data_root,
            DMOJ_PROBLEM_MEDIA_ROOT=self.media_root,
            DMOJ_PROBLEM_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILES=None,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        # Publicar un manifiesto público exige el permiso de visibilidad.
        self.publisher = create_user(username='publisher', is_superuser=True)

    def publish(self, raw, problem=None, user=None):
        return publish_package(raw, problem=problem,
                               user=self.publisher if user is None else user)

    def test_publish_valid_package(self):
        raw = package(media={'figura.png': b'png'})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            expected_data_zip = archive.read('data.zip')
        result = self.publish(raw)

        problem = Problem.objects.get(code=result.problem_id)
        self.assertRegex(problem.code, r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        self.assertEqual(problem.name, 'Suma de dos')
        self.assertEqual(problem.description, '# Enunciado\n')
        self.assertTrue(problem.is_public)
        self.assertEqual(problem.group.name, 'basico')
        self.assertEqual(set(problem.types.values_list('name', flat=True)), {'math'})
        self.assertCountEqual(result.aliases_added, ['suma-de-dos', 'suma'])
        self.assertCountEqual(problem.aliases.values_list('name', flat=True), ['suma-de-dos', 'suma'])

        self.assertEqual(problem.cases.count(), 1)
        case = problem.cases.get()
        self.assertEqual((case.order, case.type, case.input_file, case.output_file, case.points),
                         (1, 'C', '1.in', '1.out', 100))

        data = ProblemData.objects.get(problem=problem)
        self.assertEqual(data.initfile.name, '%s/init.yml' % problem.code)
        self.assertEqual(data.zipfile.name, '%s/data.zip' % problem.code)

        code_dir = os.path.join(self.data_root, problem.code)
        self.assertEqual(tree(code_dir), {
            'init.yml': INIT_YML,
            'data.zip': expected_data_zip,
        })
        self.assertEqual(tree(os.path.join(self.media_root, problem.code)), {'figura.png': b'png'})
        self.assertEqual(result.files_written, ('data.zip', 'init.yml'))
        self.assertEqual(result.media_files, ('media/figura.png',))
        self.assertEqual(result.cases, 1)
        self.assertTrue(result.created)

        # Todos los lenguajes quedan habilitados en un problema nuevo.
        self.assertEqual(set(problem.allowed_languages.values_list('key', flat=True)),
                         set(Language.objects.values_list('key', flat=True)))

    def test_missing_init_yml(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(omit=('init.yml',)))

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn('missing', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_referenced_case_missing_from_archive(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(cases={'1.in': CASE_IN}))

        self.assertEqual(caught.exception.file, '1.out')
        self.assertIn('not found', caught.exception.reason)
        self.assertIn('data.zip', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_disallowed_extension_at_package_root(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(extra={'notas.txt': b'hola'}))

        self.assertEqual(caught.exception.file, 'notas.txt')
        self.assertIn('not allowed', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_disallowed_media_extension(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(media={'malicioso.html': b'<script></script>'}))

        self.assertEqual(caught.exception.file, 'media/malicioso.html')
        self.assertIn('not allowed', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))
        self.assertFalse(os.path.exists(self.media_root))

    def test_unknown_manifest_schema(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'schema': 2})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('unknown schema', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_limits_are_applied_to_every_language_with_the_extension(self):
        result = self.publish(package())
        problem = Problem.objects.get(code=result.problem_id)

        expected = {
            '.cpp': (1.0, 262144),
            '.py': (2.5, 131072),
        }
        for extension, (time_limit, memory_limit) in expected.items():
            languages = list(Language.objects.filter(extension__iexact=extension.lstrip('.')))
            self.assertTrue(languages)
            for language in languages:
                limit = LanguageLimit.objects.get(problem=problem, language=language)
                self.assertEqual(limit.time_limit, time_limit)
                self.assertEqual(limit.memory_limit, memory_limit)

        # ``cpp`` y ``py`` están compartidas por más de un ejecutor.
        self.assertGreater(Language.objects.filter(extension__iexact='cpp').count(), 1)
        self.assertGreater(Language.objects.filter(extension__iexact='py').count(), 1)
        self.assertEqual(LanguageLimit.objects.filter(problem=problem).count(),
                         Language.objects.filter(extension__iexact='cpp').count()
                         + Language.objects.filter(extension__iexact='py').count())

    def test_republish_replaces_files_and_media_but_never_removes_aliases(self):
        first = self.publish(package(media={'viejo.png': b'old'}))
        problem = Problem.objects.get(code=first.problem_id)

        changed_manifest = yaml_manifest({'title': 'Otro título', 'aliases': ['nuevo'], 'types': ['geometry']})
        changed_init = b'archive: otro.zip\ntest_cases:\n- points: 50\n  in: 2.in\n  out: 2.out\n'
        second = self.publish(
            package(manifest=changed_manifest, init=changed_init,
                    cases={'2.in': b'2\n', '2.out': b'2\n'}, media={'nuevo.png': b'new'},
                    archive_name='otro.zip'),
            problem=problem,
        )

        self.assertEqual(second.problem_id, problem.code)
        problem.refresh_from_db()
        self.assertEqual(problem.name, 'Otro título')
        self.assertEqual(set(problem.types.values_list('name', flat=True)), {'geometry'})
        self.assertEqual(problem.cases.get().input_file, '2.in')
        self.assertEqual(set(problem.aliases.values_list('name', flat=True)), {'suma-de-dos', 'suma', 'nuevo'})
        self.assertEqual(second.aliases_added, ('nuevo',))

        code_dir = os.path.join(self.data_root, problem.code)
        self.assertEqual(tree(code_dir)['init.yml'], changed_init)
        self.assertIn('otro.zip', tree(code_dir))
        self.assertNotIn('data.zip', tree(code_dir))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, problem.code, 'viejo.png')))
        self.assertTrue(os.path.exists(os.path.join(self.media_root, problem.code, 'nuevo.png')))

        # El árbol de datos no deja temporales ni copias del paquete anterior.
        self.assertFalse([name for name in os.listdir(self.data_root) if name.startswith('.')])
        self.assertFalse([name for name in os.listdir(self.media_root) if name.startswith('.')])

    def test_publishing_the_same_bytes_twice_is_idempotent(self):
        raw = package(media={'figura.png': b'png'})
        first = self.publish(raw)
        problem = Problem.objects.get(code=first.problem_id)

        code_dir = os.path.join(self.data_root, problem.code)
        media_dir = os.path.join(self.media_root, problem.code)
        before = {
            'data': tree(code_dir),
            'media': tree(media_dir),
            'data_stat': {name: os.stat(os.path.join(code_dir, name)) for name in tree(code_dir)},
            'media_stat': {name: os.stat(os.path.join(media_dir, name)) for name in tree(media_dir)},
            'cases': list(ProblemTestCase.objects.filter(dataset=problem).order_by('order')
                          .values_list('id', 'order', 'type', 'input_file', 'output_file', 'points')),
            'aliases': list(problem.aliases.order_by('name').values_list('id', 'name')),
        }

        second = self.publish(raw, problem=problem)

        self.assertFalse(second.changed)
        self.assertEqual(second.aliases_added, ())
        self.assertEqual(tree(code_dir), before['data'])
        self.assertEqual(tree(media_dir), before['media'])
        for name, stat in before['data_stat'].items():
            current = os.stat(os.path.join(code_dir, name))
            self.assertEqual(current.st_ino, stat.st_ino)
            self.assertEqual(current.st_mtime_ns, stat.st_mtime_ns)
        for name, stat in before['media_stat'].items():
            current = os.stat(os.path.join(media_dir, name))
            self.assertEqual(current.st_ino, stat.st_ino)
            self.assertEqual(current.st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(
            list(ProblemTestCase.objects.filter(dataset=problem).order_by('order')
                 .values_list('id', 'order', 'type', 'input_file', 'output_file', 'points')),
            before['cases'],
        )
        self.assertEqual(list(problem.aliases.order_by('name').values_list('id', 'name')), before['aliases'])

    def test_failure_while_swapping_leaves_the_previous_package_intact(self):
        first_package = package(statement='viejo\n', media={'viejo.png': b'old'})
        first = self.publish(first_package)
        problem = Problem.objects.get(code=first.problem_id)
        old_data = tree(os.path.join(self.data_root, problem.code))
        old_media = tree(os.path.join(self.media_root, problem.code))
        old_cases = list(problem.cases.values_list('id', 'input_file', 'output_file'))
        old_data_row = ProblemData.objects.get(problem=problem)

        new_package = package(statement='nuevo\n', media={'nuevo.png': b'new'}, cases={
            '9.in': b'9\n', '9.out': b'9\n',
        }, init=b'archive: data.zip\ntest_cases:\n- points: 10\n  in: 9.in\n  out: 9.out\n')

        original_swap = problem_package._swap_tree
        calls = {'count': 0}

        def failing_swap(staged, live, backup):
            calls['count'] += 1
            if calls['count'] == 2:
                raise OSError('falla simulada al intercambiar medios')
            return original_swap(staged, live, backup)

        with mock.patch.object(problem_package, '_swap_tree', side_effect=failing_swap):
            with self.assertRaises(PackageError) as caught:
                self.publish(new_package, problem=problem)

        self.assertIn('falla simulada', str(caught.exception))
        self.assertEqual(tree(os.path.join(self.data_root, problem.code)), old_data)
        self.assertEqual(tree(os.path.join(self.media_root, problem.code)), old_media)
        problem.refresh_from_db()
        self.assertEqual(problem.name, 'Suma de dos')
        self.assertEqual(list(problem.cases.values_list('id', 'input_file', 'output_file')), old_cases)
        self.assertEqual(ProblemData.objects.get(problem=problem).zipfile.name, old_data_row.zipfile.name)
        self.assertFalse([name for name in os.listdir(self.data_root) if name.startswith('.')])
        self.assertFalse([name for name in os.listdir(self.media_root) if name.startswith('.')])

    @override_settings(DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE=10)
    def test_media_file_over_the_size_limit_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(media={'grande.png': b'x' * 100}))

        self.assertEqual(caught.exception.file, 'media/grande.png')
        self.assertIn('too big', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    # -- visibilidad -------------------------------------------------------

    def test_public_manifest_requires_change_public_visibility(self):
        editor = create_user(username='editor', user_permissions=('edit_own_problem',))

        with self.assertRaises(PackageError) as caught:
            self.publish(package(), user=editor)

        self.assertEqual(caught.exception.code, 'forbidden')
        self.assertIn('change_public_visibility', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_private_manifest_needs_no_visibility_permission(self):
        editor = create_user(username='editor', user_permissions=('edit_own_problem',))

        result = self.publish(package(manifest=yaml_manifest({'visibility': 'private'})), user=editor)

        self.assertFalse(Problem.objects.get(code=result.problem_id).is_public)

    def test_change_public_visibility_lets_a_plain_editor_publish_public(self):
        editor = create_user(username='editor',
                             user_permissions=('edit_own_problem', 'change_public_visibility'))

        result = self.publish(package(), user=editor)

        self.assertTrue(Problem.objects.get(code=result.problem_id).is_public)

    # -- límites del manifiesto -------------------------------------------

    def test_time_limit_over_the_model_maximum_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'time_limit': 61})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('time_limit is over the 60s limit', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_memory_limit_over_the_model_maximum_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'memory_limit': 1048577})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('memory_limit is over the 1048576KB limit', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_extension_limit_over_the_model_maximum_is_rejected(self):
        manifest = yaml_manifest({'limits': {'.cpp': {'tl': 61, 'ml': 262144}}})

        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=manifest))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('limits.cpp.tl is over the 60s limit', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    # -- archivos del juez y de casos -------------------------------------

    @override_settings(DMOJ_PROBLEM_PACKAGE_MAX_FILES=3)
    def test_too_many_files_is_rejected_by_the_service(self):
        # El paquete mínimo ya tiene cuatro archivos: manifiesto, init,
        # enunciado y el zip de casos.
        with self.assertRaises(PackageError) as caught:
            self.publish(package())

        self.assertEqual(caught.exception.code, 'too_many_files')
        self.assertIn('over the 3 limit', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_non_string_case_file_is_a_package_error(self):
        init = b'archive: data.zip\ntest_cases:\n- points: 100\n  in: [1.in]\n  out: 1.out\n'

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init))

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn('must be file names', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_batched_non_string_case_file_is_a_package_error(self):
        init = (b'archive: data.zip\ntest_cases:\n- points: 100\n'
                b'  batched:\n  - {in: {nombre: 1.in}, out: 1.out}\n')

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init))

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn('must be file names', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_signature_requires_an_evaluator(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(extra={'signature.hpp': b'int firma();'}))

        self.assertEqual(caught.exception.file, 'signature.hpp')
        self.assertIn('evaluator', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_signature_with_its_evaluator_is_accepted(self):
        result = self.publish(package(extra={
            'signature.hpp': b'int firma();',
            'evaluator.cpp': b'int main() { return 0; }',
        }))

        data = ProblemData.objects.get(problem__code=result.problem_id)
        self.assertTrue(data.signature_grading)
        self.assertTrue(data.signature.name.endswith('/signature.hpp'))
        self.assertTrue(data.evaluator.name.endswith('/evaluator.cpp'))

    # -- rescore -----------------------------------------------------------

    def test_changing_points_or_visibility_triggers_a_rescore(self):
        problem = Problem.objects.get(code=self.publish(package()).problem_id)

        with mock.patch('judge.tasks.rescore_problem') as task:
            with self.captureOnCommitCallbacks(execute=True):
                self.publish(package(manifest=yaml_manifest({'points': 50})), problem=problem)

        task.s.assert_called_once_with(problem.pk)
        task.s.return_value.delay.assert_called_once_with()

        with mock.patch('judge.tasks.rescore_problem') as task:
            with self.captureOnCommitCallbacks(execute=True):
                self.publish(package(manifest=yaml_manifest({'visibility': 'private'})), problem=problem)

        task.s.assert_called_once_with(problem.pk)
        task.s.return_value.delay.assert_called_once_with()

    def test_a_cases_only_change_does_not_trigger_a_rescore(self):
        problem = Problem.objects.get(code=self.publish(package()).problem_id)
        init = b'archive: data.zip\ntest_cases:\n- points: 10\n  in: 9.in\n  out: 9.out\n'

        with mock.patch('judge.tasks.rescore_problem') as task:
            with self.captureOnCommitCallbacks(execute=True):
                self.publish(package(init=init, cases={'9.in': b'9\n', '9.out': b'9\n'}),
                             problem=problem)

        task.s.assert_not_called()
        self.assertEqual(list(problem.cases.values_list('input_file', flat=True)), ['9.in'])
