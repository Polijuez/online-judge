import io
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from django.test import TestCase, override_settings
from reversion.models import Version

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

#: Problema real de producción con ``checker: floats`` (montaje de sólo lectura).
PRODUCTION_PROBLEM = '/mnt/nfs/progcomp/polijuez/problems/b0ec8dd6-cdcb-4da1-8473-1cfeed44d5b5'

#: Todos los checkers incorporados del juez (``dmoj.checkers``).
BUILTIN_CHECKERS = (
    'bridged', 'easy', 'floats', 'floatsabs', 'floatsrel', 'identical',
    'linecount', 'linematches', 'rstripped', 'sorted', 'standard', 'unordered',
)

MANIFEST = {
    'schema': 1,
    'title': 'Suma de dos',
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


def init_yml(**extra):
    """``init.yml`` mínimo con las claves extra del juez delante."""
    lines = [b'archive: data.zip\n']
    for key, value in extra.items():
        lines.append(yaml_dump({key: value}))
    lines.append(b'test_cases:\n- points: 100\n  in: 1.in\n  out: 1.out\n')
    return b''.join(lines)


def case_zip(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        files = files if files is not None else {'1.in': CASE_IN, '1.out': CASE_OUT}
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def package(manifest=None, init=INIT_YML, statement='# Enunciado\n', cases=None, extra=None, media=None,
            omit=(), archive_name='data.zip', statement_name='statement.md'):
    """Arma un zip de paquete; ``omit`` saca archivos por nombre.

    ``statement_name`` elige el archivo de enunciado (md o pdf); con pdf,
    ``statement`` son los bytes del archivo.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        if 'problem.yml' not in omit:
            archive.writestr('problem.yml', yaml_dump(manifest or yaml_manifest()))
        if 'init.yml' not in omit:
            archive.writestr('init.yml', init)
        if statement_name not in omit:
            if isinstance(statement, bytes):
                archive.writestr(statement_name, statement)
            else:
                archive.writestr(statement_name, statement.encode('utf-8'))
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


def production_file(name):
    with open(os.path.join(PRODUCTION_PROBLEM, name), 'rb') as f:
        return f.read()


def production_snapshot():
    """Nombre, tamaño y mtime de cada archivo del problema de producción."""
    snapshot = {}
    for current, _, filenames in os.walk(PRODUCTION_PROBLEM):
        for filename in filenames:
            path = os.path.join(current, filename)
            stat = os.stat(path)
            snapshot[os.path.relpath(path, PRODUCTION_PROBLEM)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def package_from_directory(root):
    """Arma el zip del paquete a partir del árbol de un problema publicado.

    El árbol de un problema de producción guarda ``init.yml``, el zip de casos y
    los archivos del juez, más artefactos que no son parte del paquete
    (``generator.py``, ``solution.cpp``) y el enunciado en
    ``enunciado/enunciado.md``. Se copia a memoria solo lo que el paquete
    admite —``init.yml``, su ``archive`` y los archivos que ``init.yml``
    nombre— y nunca se escribe en el directorio de origen.
    """
    import yaml

    with open(os.path.join(root, 'init.yml'), 'rb') as f:
        init_bytes = f.read()
    init_data = yaml.safe_load(init_bytes) or {}

    names = ['init.yml']
    if isinstance(init_data.get('archive'), str):
        names.append(init_data['archive'])
    for key in problem_package.INIT_FILE_KEYS:
        value = init_data.get(key)
        name = value.get('name') if isinstance(value, dict) else value
        if isinstance(name, str) and os.path.isfile(os.path.join(root, name)):
            names.append(name)

    statement = os.path.join(root, 'enunciado', 'enunciado.md')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in dict.fromkeys(names):
            archive.write(os.path.join(root, name), name)
        if os.path.isfile(statement):
            archive.write(statement, 'statement.md')
        archive.writestr('problem.yml', yaml_dump(yaml_manifest({
            'title': 'Unión de círculos', 'types': ['geometry'],
        })))
    return buffer.getvalue()


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
        self.assertFalse(problem.is_full_markup)
        self.assertTrue(problem.is_public)
        self.assertEqual(problem.group.name, 'basico')
        self.assertEqual(set(problem.types.values_list('name', flat=True)), {'math'})

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
        self.assertEqual(tree(os.path.join(self.media_root, problem.code)),
                         {'figura.png': b'png', 'statement.md': b'# Enunciado\n'})
        self.assertEqual(result.files_written, ('data.zip', 'init.yml'))
        self.assertEqual(result.media_files, ('media/figura.png', 'media/statement.md'))
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

    def test_statement_file_also_lands_in_the_media_dir(self):
        result = self.publish(package(media={'figura.png': b'png'}))
        problem = Problem.objects.get(code=result.problem_id)

        self.assertEqual(
            tree(os.path.join(self.media_root, problem.code)),
            {'figura.png': b'png', 'statement.md': b'# Enunciado\n'},
        )
        self.assertIn('media/statement.md', result.media_files)

    def test_package_without_any_statement_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(omit=('statement.md',)))

        self.assertIn('statement', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_package_with_both_statements_is_rejected(self):
        buffer = io.BytesIO(package(statement=b'%PDF-1.4\n', statement_name='statement.pdf'))
        with zipfile.ZipFile(buffer, 'a') as archive:
            archive.writestr('statement.md', '# otro\n')
        raw = buffer.getvalue()
        with self.assertRaises(PackageError) as caught:
            self.publish(raw)

        self.assertIn('exactly one', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_pdf_statement_publishes_an_embed_and_marks_full_markup(self):
        raw = package(statement=b'%PDF-1.4 fake\n', statement_name='statement.pdf')
        result = self.publish(raw)
        problem = Problem.objects.get(code=result.problem_id)

        self.assertTrue(problem.is_full_markup)
        self.assertEqual(
            problem.description,
            '<embed style="overflow: scroll; height: 75vh; width: 100%; pointer-events: auto;" '
            'src="/media/problems/{}/statement.pdf">'.format(problem.code),
        )
        self.assertEqual(
            tree(os.path.join(self.media_root, problem.code)),
            {'statement.pdf': b'%PDF-1.4 fake\n'},
        )

    def test_replacing_a_pdf_statement_with_markdown_clears_full_markup(self):
        first = self.publish(package(statement=b'%PDF-1.4\n', statement_name='statement.pdf'))
        problem = Problem.objects.get(code=first.problem_id)
        self.assertTrue(problem.is_full_markup)

        self.publish(package(), problem=problem)
        problem.refresh_from_db()
        self.assertFalse(problem.is_full_markup)

    def test_relative_statement_references_are_rewritten_to_the_problem_media_url(self):
        statement = (
            '# Enunciado\n\n'
            'Leé [la plantilla](media/template.hs) y [el kit](kit.zip).\n\n'
            '![dibujo](media/dibujo.png)\n\n'
            'Absoluto [DMOJ](https://dmoj.ca) y [interno](/problems/) y [ancla](#seccion).\n'
        )
        result = self.publish(package(statement=statement, media={
            'template.hs': b'f =', 'kit.zip': b'PK', 'dibujo.png': b'png',
        }))
        problem = Problem.objects.get(code=result.problem_id)

        base = '/media/problems/%s/' % problem.code
        self.assertEqual(
            problem.description,
            '# Enunciado\n\n'
            'Leé [la plantilla](%stemplate.hs) y [el kit](%skit.zip).\n\n'
            '![dibujo](%sdibujo.png)\n\n'
            'Absoluto [DMOJ](https://dmoj.ca) y [interno](/problems/) y [ancla](#seccion).\n' % (
                base, base, base,
            ),
        )
        self.assertFalse(any('statement references' in warning for warning in result.warnings))

    def test_statement_reference_missing_from_media_is_a_warning(self):
        result = self.publish(package(statement='Leé [esto](template.hs)\n'))

        self.assertTrue(any('template.hs' in warning for warning in result.warnings))

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

    def test_republish_replaces_files_and_media(self):
        first = self.publish(package(media={'viejo.png': b'old'}))
        problem = Problem.objects.get(code=first.problem_id)

        changed_manifest = yaml_manifest({'title': 'Otro título', 'types': ['geometry']})
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
        }

        second = self.publish(raw, problem=problem)

        self.assertFalse(second.changed)
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

    # -- checkers incorporados del juez ------------------------------------

    def test_the_builtin_checker_set_is_the_one_the_judge_ships(self):
        # Los 12 módulos de ``dmoj.checkers``; si el juez agrega uno, esta
        # lista y ``BUILTIN_CHECKERS`` se mueven juntas.
        self.assertEqual(problem_package.BUILTIN_CHECKERS, frozenset(BUILTIN_CHECKERS))

    def test_every_builtin_checker_is_accepted_without_a_checker_file(self):
        for name in BUILTIN_CHECKERS:
            with self.subTest(checker=name):
                result = self.publish(package(init=init_yml(checker=name)))

                data = ProblemData.objects.get(problem__code=result.problem_id)
                # El checker incorporado no es un archivo: el juez lo resuelve
                # por nombre desde ``dmoj.checkers``.
                self.assertFalse(data.checker)
                self.assertFalse(data.signature_grading)
                published = tree(os.path.join(self.data_root, result.problem_id))
                self.assertEqual(sorted(published), ['data.zip', 'init.yml'])
                self.assertEqual(published['init.yml'], init_yml(checker=name))

    def test_unknown_builtin_checker_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init_yml(checker='flotantes')))

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn("unknown built-in checker 'flotantes'", caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_builtin_checker_cannot_be_combined_with_checker_py(self):
        checker = b'def check(proc, output, point_value, **kwargs):\n    return True\n'

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init_yml(checker='floats'), extra={'checker.py': checker}))

        self.assertEqual(caught.exception.file, 'checker.py')
        self.assertIn('ambiguous checker', caught.exception.reason)
        self.assertIn('floats', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_named_checker_file_is_still_required_and_extension_checked(self):
        checker = b'def check(proc, output, point_value, **kwargs):\n    return True\n'

        result = self.publish(package(init=init_yml(checker='mio.py'),
                                      extra={'mio.py': checker}))
        data = ProblemData.objects.get(problem__code=result.problem_id)
        self.assertTrue(data.checker.name.endswith('/mio.py'))

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init_yml(checker='mio.rb')))
        self.assertEqual(caught.exception.file, 'mio.rb')
        self.assertIn('extension is not allowed for checker', caught.exception.reason)

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init_yml(checker='mio.py')))
        self.assertEqual(caught.exception.file, 'mio.py')
        self.assertIn('not present in the package', caught.exception.reason)

    def test_a_builtin_declared_as_a_mapping_is_resolved_by_name(self):
        # El juez acepta ``checker: {name: floats, args: {...}}``; el bloque
        # sigue nombrando un incorporado, así que tampoco es un archivo.
        init = init_yml(checker={'name': 'floats', 'args': {'error_margin': 1e-6}})

        result = self.publish(package(init=init))

        data = ProblemData.objects.get(problem__code=result.problem_id)
        self.assertFalse(data.checker)
        self.assertEqual(sorted(tree(os.path.join(self.data_root, result.problem_id))),
                         ['data.zip', 'init.yml'])

    def test_republishing_with_a_builtin_clears_a_previous_checker_file(self):
        checker = b'def check(proc, output, point_value, **kwargs):\n    return True\n'
        problem = Problem.objects.get(code=self.publish(package(
            init=init_yml(checker='mio.py'), extra={'mio.py': checker},
        )).problem_id)
        self.assertTrue(ProblemData.objects.get(problem=problem).checker)

        self.publish(package(init=init_yml(checker='floats')), problem=problem)

        self.assertFalse(ProblemData.objects.get(problem=problem).checker)
        self.assertEqual(sorted(tree(os.path.join(self.data_root, problem.code))),
                         ['data.zip', 'init.yml'])

    def test_two_files_for_one_field_are_rejected_as_ambiguous(self):
        # ``signature``/``evaluator`` nombrados por ``init.yml`` y además el
        # bloque ``signature_grader`` con otros nombres: el mismo campo de
        # ``ProblemData`` no puede salir de dos archivos distintos.
        init = init_yml(signature='firma.hpp', evaluator='evaluador.cpp',
                        signature_grader={'entry': 'otro.cpp', 'header': 'otra.hpp'})

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init, extra={
                'firma.hpp': b'int f();', 'evaluador.cpp': b'int main() { return 0; }',
                'otro.cpp': b'int main() { return 0; }', 'otra.hpp': b'int f();',
            }))

        self.assertIn('ambiguous', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_the_canonical_file_alongside_another_name_for_the_same_field_is_ambiguous(self):
        checker = b'def check(proc, output, point_value, **kwargs):\n    return True\n'
        grader = b'def grade(proc, output, point_value, **kwargs):\n    return True\n'
        cases = (
            ('checker', init_yml(checker='mio.py'),
             {'checker.py': checker, 'mio.py': checker}, 'mio.py'),
            ('grader', init_yml(grader='mio.py'),
             {'grader.py': grader, 'mio.py': grader}, 'mio.py'),
            ('custom_judge against grader', init_yml(custom_judge='judge.py'),
             {'grader.py': grader, 'judge.py': grader}, 'judge.py'),
        )
        for label, init, extra, file in cases:
            with self.subTest(case=label):
                with self.assertRaises(PackageError) as caught:
                    self.publish(package(init=init, extra=extra))

                self.assertEqual(caught.exception.file, file)
                self.assertIn('ambiguous', caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)
                self.assertFalse(os.path.exists(self.data_root))

    def test_the_same_canonical_file_is_not_ambiguous(self):
        # ``signature_grader`` apuntando a los nombres canónicos es el mismo
        # archivo para el mismo campo: publicarlo dos veces no es un conflicto.
        init = init_yml(signature_grader={'entry': 'evaluator.cpp', 'header': 'signature.hpp'})

        result = self.publish(package(init=init, extra={
            'signature.hpp': b'int firma();',
            'evaluator.cpp': b'int main() { return 0; }',
        }))

        data = ProblemData.objects.get(problem__code=result.problem_id)
        self.assertTrue(data.signature_grading)
        self.assertTrue(data.signature.name.endswith('/signature.hpp'))
        self.assertTrue(data.evaluator.name.endswith('/evaluator.cpp'))

    # -- signature_grader --------------------------------------------------

    def test_signature_grader_wires_entry_to_the_evaluator_and_header_to_the_signature(self):
        init = init_yml(signature_grader={'entry': 'grader.cpp', 'header': 'grader.hpp'})
        result = self.publish(package(init=init, extra={
            'grader.cpp': b'int main() { return 0; }',
            'grader.hpp': b'int resolver(int);\n',
        }))

        data = ProblemData.objects.get(problem__code=result.problem_id)
        self.assertTrue(data.signature_grading)
        self.assertTrue(data.evaluator.name.endswith('/grader.cpp'))
        self.assertTrue(data.signature.name.endswith('/grader.hpp'))
        self.assertEqual(tree(os.path.join(self.data_root, result.problem_id)), {
            'init.yml': init,
            'data.zip': case_zip(),
            'grader.cpp': b'int main() { return 0; }',
            'grader.hpp': b'int resolver(int);\n',
        })

    def test_signature_grader_accepts_allow_main(self):
        init = init_yml(signature_grader={'entry': 'grader.cpp', 'header': 'grader.hpp', 'allow_main': True})
        result = self.publish(package(init=init, extra={
            'grader.cpp': b'int main() { return 0; }',
            'grader.hpp': b'int resolver(int);\n',
        }))

        self.assertTrue(ProblemData.objects.get(problem__code=result.problem_id).signature_grading)

    def test_signature_grader_without_the_pair_is_rejected(self):
        cases = {
            'entry only': {'entry': 'grader.cpp'},
            'header only': {'header': 'grader.hpp'},
            'neither': {},
        }
        for label, block in cases.items():
            with self.subTest(block=label):
                with self.assertRaises(PackageError) as caught:
                    self.publish(package(init=init_yml(signature_grader=block), extra={
                        'grader.cpp': b'int main() { return 0; }',
                        'grader.hpp': b'int resolver(int);\n',
                    }))

                self.assertEqual(caught.exception.file, 'init.yml')
                self.assertIn('required', caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)
                self.assertFalse(os.path.exists(self.data_root))

    def test_signature_grader_rejects_missing_files_wrong_extensions_and_traversal(self):
        cases = (
            ('missing entry', {'entry': 'falta.cpp', 'header': 'grader.hpp'}, 'falta.cpp',
             'not present in the package'),
            ('missing header', {'entry': 'grader.cpp', 'header': 'falta.hpp'}, 'falta.hpp',
             'not present in the package'),
            ('entry extension', {'entry': 'grader.py', 'header': 'grader.hpp'}, 'grader.py',
             'extension is not allowed for signature_grader.entry'),
            ('header extension', {'entry': 'grader.cpp', 'header': 'grader.h'}, 'grader.h',
             'extension is not allowed for signature_grader.header'),
            ('entry traversal', {'entry': '../grader.cpp', 'header': 'grader.hpp'}, '../grader.cpp',
             'invalid file reference in init.yml'),
            ('header traversal', {'entry': 'grader.cpp', 'header': '../grader.hpp'}, '../grader.hpp',
             'invalid file reference in init.yml'),
            ('entry not a string', {'entry': ['grader.cpp'], 'header': 'grader.hpp'}, 'init.yml',
             'signature_grader.entry is required'),
            # ``init.yml`` viaja tal cual al juez, que resuelve el nombre crudo:
            # aceptar el nombre recortado publicaría un problema que no califica.
            ('entry padded', {'entry': ' grader.cpp', 'header': 'grader.hpp'}, ' grader.cpp',
             'must not have surrounding whitespace'),
            ('header padded', {'entry': 'grader.cpp', 'header': 'grader.hpp '}, 'grader.hpp ',
             'must not have surrounding whitespace'),
        )
        for label, block, file, reason in cases:
            with self.subTest(case=label):
                with self.assertRaises(PackageError) as caught:
                    self.publish(package(init=init_yml(signature_grader=block), extra={
                        'grader.cpp': b'int main() { return 0; }',
                        'grader.hpp': b'int resolver(int);\n',
                    }))

                self.assertEqual(caught.exception.file, file)
                self.assertIn(reason, caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)
                self.assertFalse(os.path.exists(self.data_root))

    def test_signature_grader_rejects_unknown_keys_and_a_non_bool_allow_main(self):
        cases = (
            ({'entry': 'grader.cpp', 'header': 'grader.hpp', 'allow_main': 'sí'},
             'signature_grader.allow_main must be a boolean'),
            ({'entry': 'grader.cpp', 'header': 'grader.hpp', 'main': True},
             'unknown key(s) in signature_grader: main'),
            ('grader.cpp', 'signature_grader must be a mapping with entry and header'),
        )
        for block, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(PackageError) as caught:
                    self.publish(package(init=init_yml(signature_grader=block), extra={
                        'grader.cpp': b'int main() { return 0; }',
                        'grader.hpp': b'int resolver(int);\n',
                    }))

                self.assertEqual(caught.exception.file, 'init.yml')
                self.assertIn(reason, caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)

    def test_custom_judge_cannot_be_combined_with_signature_grader(self):
        init = init_yml(custom_judge='judge.py',
                        signature_grader={'entry': 'grader.cpp', 'header': 'grader.hpp'})

        with self.assertRaises(PackageError) as caught:
            self.publish(package(init=init, extra={
                'judge.py': b'def check(proc, output, point_value, **kwargs):\n    return True\n',
                'grader.cpp': b'int main() { return 0; }',
                'grader.hpp': b'int resolver(int);\n',
            }))

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn('custom_judge cannot be combined with signature_grader', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_the_custom_judge_conflict_is_declared_by_key_not_by_a_resolvable_name(self):
        # El juez prefiere ``CustomGrader`` con solo ver la clave ``custom_judge``,
        # aunque su valor no nombre nada: declararla junto al bloque deja el
        # paquete sin un modo de calificar.
        for value in (None, {}, [], ''):
            with self.subTest(custom_judge=value):
                init = init_yml(custom_judge=value,
                                signature_grader={'entry': 'grader.cpp', 'header': 'grader.hpp'})

                with self.assertRaises(PackageError) as caught:
                    self.publish(package(init=init, extra={
                        'grader.cpp': b'int main() { return 0; }',
                        'grader.hpp': b'int resolver(int);\n',
                    }))

                self.assertEqual(caught.exception.file, 'init.yml')
                self.assertIn('custom_judge cannot be combined with signature_grader',
                              caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)

    def test_republishing_without_signature_grader_clears_the_fields_and_the_files(self):
        init = init_yml(signature_grader={'entry': 'grader.cpp', 'header': 'grader.hpp'})
        first = self.publish(package(init=init, extra={
            'grader.cpp': b'int main() { return 0; }',
            'grader.hpp': b'int resolver(int);\n',
        }))
        problem = Problem.objects.get(code=first.problem_id)
        self.assertTrue(ProblemData.objects.get(problem=problem).signature_grading)

        self.publish(package(), problem=problem)

        data = ProblemData.objects.get(problem=problem)
        self.assertFalse(data.signature_grading)
        self.assertFalse(data.signature)
        self.assertFalse(data.evaluator)
        self.assertFalse(data.checker)
        self.assertEqual(tree(os.path.join(self.data_root, problem.code)), {
            'init.yml': INIT_YML,
            'data.zip': case_zip(),
        })
        self.assertIn('grader.cpp', first.files_written)

    # -- visibilidad -------------------------------------------------------

    def test_a_manifest_without_visibility_is_rejected(self):
        manifest = yaml_manifest()
        manifest.pop('visibility')

        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=manifest))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('visibility is required', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_an_unknown_visibility_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'visibility': 'interno'})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('visibility must be one of', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    # -- testers -----------------------------------------------------------

    def test_the_manifest_accepts_testers_and_still_rejects_unknown_keys(self):
        tester = create_user(username='examen-tester')

        result = self.publish(package(manifest=yaml_manifest({'testers': ['examen-tester']})))

        problem = Problem.objects.get(code=result.problem_id)
        self.assertEqual(list(problem.testers.values_list('user__username', flat=True)), ['examen-tester'])
        self.assertEqual(result.testers, ('examen-tester',))
        self.assertEqual(tester.id, problem.testers.get().user_id)

        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'aliases': ['suma']})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('unknown key(s): aliases', caught.exception.reason)

    def test_a_created_problem_gets_the_testers_the_package_declares(self):
        create_user(username='ana')
        create_user(username='beto')

        result = self.publish(package(manifest=yaml_manifest({'testers': ['beto', 'ana']})))

        problem = Problem.objects.get(code=result.problem_id)
        self.assertEqual(
            list(problem.testers.order_by('user__username').values_list('user__username', flat=True)),
            ['ana', 'beto'],
        )
        # El resultado informa lo que quedó, ordenado por username.
        self.assertEqual(result.testers, ('ana', 'beto'))

    def test_an_unknown_tester_rejects_the_package_naming_every_offender(self):
        create_user(username='ana')

        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'testers': ['ana', 'fantasma', 'otro-fantasma']})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('unknown username(s): fantasma, otro-fantasma', caught.exception.reason)
        # Nada quedó escrito: ni fila, ni directorios, ni la relación M2M.
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))
        self.assertFalse(os.path.exists(self.media_root))

    def test_tester_usernames_are_exact_and_case_sensitive(self):
        create_user(username='ana')

        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'testers': ['Ana']})))

        self.assertIn('unknown username(s): Ana', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_testers_must_be_a_list_of_usernames(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(manifest=yaml_manifest({'testers': 'ana'})))

        self.assertEqual(caught.exception.file, 'problem.yml')
        self.assertIn('testers must be a list of usernames', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_an_absent_testers_key_leaves_the_stored_set_untouched(self):
        create_user(username='ana')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)

        result = self.publish(package(manifest=yaml_manifest({'title': 'Otro título'})), problem=problem)

        self.assertEqual(list(problem.testers.values_list('user__username', flat=True)), ['ana'])
        self.assertEqual(result.testers, ('ana',))

    def test_a_non_empty_testers_list_replaces_the_stored_set(self):
        create_user(username='ana')
        create_user(username='beto')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)

        self.publish(package(manifest=yaml_manifest({'testers': ['beto']})), problem=problem)

        self.assertEqual(list(problem.testers.values_list('user__username', flat=True)), ['beto'])

    def test_an_empty_testers_list_clears_the_stored_set(self):
        create_user(username='ana')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)

        result = self.publish(package(manifest=yaml_manifest({'testers': []})), problem=problem)

        self.assertFalse(problem.testers.exists())
        self.assertEqual(result.testers, ())

    def test_republishing_the_same_testers_does_not_report_a_change(self):
        create_user(username='ana')
        raw = package(manifest=yaml_manifest({'testers': ['ana']}))
        problem = Problem.objects.get(code=self.publish(raw).problem_id)

        second = self.publish(raw, problem=problem)

        self.assertFalse(second.changed)
        self.assertEqual(second.testers, ('ana',))

    def test_a_tester_change_reports_the_package_as_changed(self):
        create_user(username='ana')
        create_user(username='beto')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)

        result = self.publish(package(manifest=yaml_manifest({'testers': ['beto']})), problem=problem)

        self.assertTrue(result.changed)

    def test_a_failure_after_resolution_rolls_the_testers_back(self):
        create_user(username='ana')
        create_user(username='beto')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)
        old_media = tree(os.path.join(self.media_root, problem.code))

        new_package = package(manifest=yaml_manifest({'testers': ['beto']}),
                              statement='nuevo\n', media={'nuevo.png': b'new'})

        def failing_swap(staged, live, backup):
            raise OSError('falla simulada al intercambiar')

        with mock.patch.object(problem_package, '_swap_tree', side_effect=failing_swap):
            with self.assertRaises(PackageError) as caught:
                self.publish(new_package, problem=problem)

        self.assertIn('falla simulada', str(caught.exception))
        # Los testers se escribieron en la misma transacción que el intercambio:
        # el fallo los deshace junto con todo lo demás.
        self.assertEqual(list(problem.testers.values_list('user__username', flat=True)), ['ana'])
        self.assertEqual(tree(os.path.join(self.media_root, problem.code)), old_media)

    def test_applying_testers_is_recorded_in_the_problem_history(self):
        create_user(username='ana')
        problem = Problem.objects.get(code=self.publish(
            package(manifest=yaml_manifest({'testers': ['ana']})),
        ).problem_id)

        history = Version.objects.get_for_object(problem).order_by('revision')

        self.assertTrue(history.exists())
        self.assertIn('testers', history.last().revision.comment.lower())
        self.assertIn('ana', history.last().revision.comment)

    # -- robustez del zip --------------------------------------------------

    def test_a_root_entry_with_a_traversal_path_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(extra={'../checker.py': b'x'}))

        self.assertEqual(caught.exception.file, '../checker.py')
        self.assertIn('invalid path in package', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_duplicated_entry_is_rejected(self):
        buffer = io.BytesIO(package())
        with zipfile.ZipFile(buffer, 'a') as archive:
            archive.writestr('init.yml', INIT_YML)

        with self.assertRaises(PackageError) as caught:
            self.publish(buffer.getvalue())

        self.assertEqual(caught.exception.file, 'init.yml')
        self.assertIn('duplicated entry', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_symlink_and_encrypted_entries_are_rejected(self):
        cases = {
            'symlink': (self.zip_with_attributes('link.py', symlink=True), 'link.py',
                        'symlinks are not accepted'),
            'encrypted': (self.zip_with_encrypted_flag('secret.py'), 'secret.py',
                          'encrypted entries are not accepted'),
        }
        for label, (raw, file, reason) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(PackageError) as caught:
                    self.publish(raw)

                self.assertEqual(caught.exception.file, file)
                self.assertIn(reason, caught.exception.reason)
                self.assertEqual(Problem.objects.count(), 0)
                self.assertFalse(os.path.exists(self.data_root))

    def test_the_symlink_and_encryption_checks_also_cover_the_case_archive(self):
        symlinked = self.zip_with_attributes('1.in', symlink=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            with zipfile.ZipFile(io.BytesIO(package(omit=('data.zip',)))) as source:
                for info in source.infolist():
                    archive.writestr(info, source.read(info))
            archive.writestr('data.zip', symlinked)

        with self.assertRaises(PackageError) as caught:
            self.publish(buffer.getvalue())

        self.assertEqual(caught.exception.file, 'data.zip')
        self.assertIn('symlinks are not accepted', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    def test_the_file_size_limit_covers_the_package_entries(self):
        with override_settings(DMOJ_PROBLEM_MAX_FILE_SIZE=400):
            with self.assertRaises(PackageError) as caught:
                self.publish(package(extra={'checker.py': b'x' * 500}))

        self.assertEqual(caught.exception.file, 'checker.py')
        self.assertIn('too big', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_file_outside_the_allowed_names_is_rejected(self):
        with self.assertRaises(PackageError) as caught:
            self.publish(package(extra={'solucion.cpp': b'int main() {}'}))

        self.assertEqual(caught.exception.file, 'solucion.cpp')
        self.assertIn('not allowed at the package root', caught.exception.reason)
        self.assertEqual(Problem.objects.count(), 0)

    # -- paquete real de producción ---------------------------------------

    @unittest.skipUnless(os.path.isdir(PRODUCTION_PROBLEM), 'production mount is not available')
    def test_the_production_package_with_the_floats_builtin_is_accepted(self):
        before = production_snapshot()
        raw = package_from_directory(PRODUCTION_PROBLEM)

        result = self.publish(raw)

        problem = Problem.objects.get(code=result.problem_id)
        data = ProblemData.objects.get(problem=problem)
        # ``checker: floats`` es un incorporado: no hay archivo ni campo.
        self.assertFalse(data.checker)
        self.assertFalse(data.signature_grading)
        self.assertFalse(data.signature)
        self.assertFalse(data.evaluator)
        # El ``init.yml`` viaja tal cual y los 37 subcasos se convierten en
        # filas C más el cierre de cada batch.
        code_dir = os.path.join(self.data_root, problem.code)
        self.assertEqual(tree(code_dir)['init.yml'], production_file('init.yml'))
        self.assertEqual(tree(code_dir)['venn.zip'], production_file('venn.zip'))
        self.assertEqual(problem.cases.filter(type='C').count(), 37)
        # El montaje de producción es de sólo lectura y no se tocó.
        self.assertEqual(production_snapshot(), before)

    # -- ayudas de los tests de robustez -----------------------------------

    def zip_with_attributes(self, name, symlink=False):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = 0o120777 << 16
            archive.writestr(info, b'contenido')
        return buffer.getvalue()

    def zip_with_encrypted_flag(self, name):
        """Marca una entrada como cifrada sin cifrar de verdad los bytes."""
        buffer = io.BytesIO(self.zip_with_attributes(name))
        raw = bytearray(buffer.getvalue())
        for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
            start = 0
            while True:
                index = raw.find(signature, start)
                if index < 0:
                    break
                raw[index + offset] |= 0x1
                start = index + 4
        return bytes(raw)

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
