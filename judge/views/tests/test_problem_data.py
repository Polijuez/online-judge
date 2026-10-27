import io
import os
import shutil
import tempfile
import uuid
import zipfile

import yaml

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import ProblemData
from judge.models.tests.util import create_problem, create_user
from judge.utils.tests.fake_judge import FakeJudgeControlServer

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

INIT_YML = b'archive: data.zip\ntest_cases:\n- points: 100\n  in: 1.in\n  out: 1.out\n'


def make_package(manifest=None, init=INIT_YML, statement='# Enunciado\n', cases=None, media=None, omit=()):
    data = dict(MANIFEST)
    data.update(manifest or {})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        if 'problem.yml' not in omit:
            archive.writestr('problem.yml', yaml.safe_dump(data).encode('utf-8'))
        if 'init.yml' not in omit:
            archive.writestr('init.yml', init)
        if 'statement.md' not in omit:
            archive.writestr('statement.md', statement)
        if 'data.zip' not in omit:
            case_buffer = io.BytesIO()
            with zipfile.ZipFile(case_buffer, 'w') as case_archive:
                for name, content in (cases or {'1.in': b'1 2\n', '1.out': b'3\n'}).items():
                    case_archive.writestr(name, content)
            archive.writestr('data.zip', case_buffer.getvalue())
        for name, content in (media or {}).items():
            archive.writestr('media/' + name, content)
    return buffer.getvalue()


def read_tree(root, code):
    directory = os.path.join(root, code)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            relative = os.path.relpath(path, directory).replace(os.sep, '/')
            with open(path, 'rb') as file:
                tree[relative] = file.read()
    return tree


class ProblemDataViewTestCase(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='problem-data-view-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = os.path.join(self.tmp, 'data')
        self.media_root = os.path.join(self.tmp, 'media', 'problems')
        self.settings = override_settings(
            MEDIA_ROOT=os.path.join(self.tmp, 'media'),
            MEDIA_URL='/media/',
            DMOJ_PROBLEM_DATA_ROOT=self.data_root,
            DMOJ_PROBLEM_MEDIA_ROOT=self.media_root,
            DMOJ_PROBLEM_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILES=None,
            DMOJ_JUDGE_CONTROL_ENDPOINTS=[],
            DMOJ_JUDGE_CONTROL_TIMEOUT=0.5,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

        self.user = create_user(username='problemsetter',
                                user_permissions=('edit_own_problem', 'change_public_visibility'))
        self.token = 'Bearer ' + self.user.profile.generate_api_token()
        self.client.force_login(self.user)

    # -- ayudas -----------------------------------------------------------

    def make_problem(self):
        return create_problem(code=str(uuid.uuid4()), name='Plantilla', authors=['problemsetter'])

    def post_package(self, problem, raw):
        return self.client.post(
            reverse('problem_data', args=[problem.code]),
            {'package': SimpleUploadedFile('package.zip', raw, content_type='application/zip')},
        )

    def put_package(self, problem, raw):
        return self.client.put(
            reverse('api_problem_replace', args=[problem.code]), data=raw,
            content_type='application/zip', HTTP_AUTHORIZATION=self.token,
        )

    def snapshot(self, problem):
        data = ProblemData.objects.filter(problem=problem).first()
        return {
            'name': problem.name,
            'description': problem.description,
            'group': problem.group.name,
            'is_public': problem.is_public,
            'time_limit': problem.time_limit,
            'memory_limit': problem.memory_limit,
            'points': problem.points,
            'types': sorted(problem.types.values_list('name', flat=True)),
            'limits': sorted(
                (limit.language.extension, limit.time_limit, limit.memory_limit)
                for limit in problem.language_limits.all()
            ),
            'cases': list(problem.cases.order_by('order').values_list(
                'order', 'type', 'input_file', 'output_file', 'points',
            )),
            'aliases': sorted(problem.aliases.values_list('name', flat=True)),
            'data_files': {
                field: os.path.basename(getattr(data, field).name) if data and getattr(data, field) else None
                for field in ProblemData.get_file_fields()
            },
            'data': read_tree(self.data_root, problem.code),
            'media': read_tree(self.media_root, problem.code),
        }

    # -- el formulario es el mismo camino que la API ----------------------

    def test_publishing_by_form_leaves_the_same_state_as_the_api(self):
        raw = make_package(media={'figura.png': b'png'})
        by_form = self.make_problem()
        by_api = self.make_problem()
        self.assertEqual(self.snapshot(by_form), self.snapshot(by_api))

        response = self.post_package(by_form, raw)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Suma de dos')
        self.assertContains(response, 'suma-de-dos')
        self.assertContains(response, 'data.zip')
        form_state = self.snapshot(by_form)

        # El paquete es el mismo: se borra el problema para liberar los alias y
        # se publica por la API sobre el problema gemelo.
        by_form.delete()
        shutil.rmtree(os.path.join(self.data_root, by_form.code), ignore_errors=True)
        shutil.rmtree(os.path.join(self.media_root, by_form.code), ignore_errors=True)
        response = self.put_package(by_api, raw)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(form_state, self.snapshot(by_api))

    def test_a_repeated_publication_is_changed_only_the_first_time(self):
        problem = self.make_problem()
        raw = make_package()

        first = self.post_package(problem, raw)
        second = self.post_package(problem, raw)

        self.assertContains(first, 'Changed')
        self.assertContains(first, 'yes')
        self.assertContains(second, 'no')

    # -- paquete inválido --------------------------------------------------

    def test_invalid_package_shows_the_reason_and_writes_nothing(self):
        problem = self.make_problem()
        before = self.snapshot(problem)
        raw = make_package(omit=('init.yml',))

        response = self.post_package(problem, raw)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'missing required file')
        self.assertContains(response, 'init.yml')
        self.assertEqual(self.snapshot(problem), before)
        self.assertFalse(ProblemData.objects.filter(problem=problem).exists())
        self.assertEqual(problem.cases.count(), 0)
        self.assertFalse(problem.aliases.exists())
        self.assertFalse(os.path.exists(os.path.join(self.data_root, problem.code)))
        self.assertFalse(os.path.exists(os.path.join(self.media_root, problem.code)))

    def test_a_file_that_is_not_a_zip_is_rejected_on_screen(self):
        problem = self.make_problem()

        response = self.client.post(
            reverse('problem_data', args=[problem.code]),
            {'package': SimpleUploadedFile('package.zip', b'not a zip at all')},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid zip file')
        self.assertFalse(os.path.exists(os.path.join(self.data_root, problem.code)))

    # -- permisos y límites ------------------------------------------------

    def test_publishing_public_without_the_permission_shows_the_reason(self):
        plain = create_user(username='plain', user_permissions=('edit_own_problem',))
        problem = create_problem(code=str(uuid.uuid4()), name='Plantilla', authors=['plain'])
        self.client.force_login(plain)

        response = self.post_package(problem, make_package())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'change_public_visibility')
        self.assertFalse(os.path.exists(os.path.join(self.data_root, problem.code)))
        self.assertEqual(problem.cases.count(), 0)

    @override_settings(DMOJ_PROBLEM_PACKAGE_MAX_FILES=3)
    def test_the_file_limit_is_enforced_by_the_form(self):
        problem = self.make_problem()

        response = self.post_package(problem, make_package())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'over the 3 limit')
        self.assertFalse(os.path.exists(os.path.join(self.data_root, problem.code)))
        self.assertEqual(problem.cases.count(), 0)

    def test_publishing_by_form_warns_the_judges(self):
        judge = FakeJudgeControlServer()
        self.addCleanup(judge.close)
        problem = self.make_problem()

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url]):
            response = self.post_package(problem, make_package())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(judge.requests, ['/update/problems'])

    # -- pantalla de medios ------------------------------------------------

    def test_media_screen_lists_the_published_files_with_their_served_urls(self):
        problem = self.make_problem()
        self.assertEqual(self.post_package(problem, make_package(media={
            'figura.png': b'png',
            'sub/otra.png': b'otra',
        })).status_code, 200)

        response = self.client.get(reverse('problem_media', args=[problem.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'figura.png')
        self.assertContains(response, 'sub/otra.png')
        self.assertContains(response, '/media/problems/%s/figura.png' % problem.code)
        self.assertContains(response, '/media/problems/%s/sub/otra.png' % problem.code)

    def test_media_screen_does_not_accept_uploads(self):
        problem = self.make_problem()
        self.assertEqual(self.post_package(problem, make_package()).status_code, 200)

        response = self.client.post(reverse('problem_media', args=[problem.code]))

        self.assertEqual(response.status_code, 405)

    # -- permisos ----------------------------------------------------------

    def test_screens_keep_the_staff_gate(self):
        problem = self.make_problem()
        self.client.logout()

        for name in ('problem_data', 'problem_media'):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[problem.code]))
                self.assertEqual(response.status_code, 302)
                self.assertIn('/accounts/login/', response['Location'])

        outsider = create_user(username='lurker')
        self.client.force_login(outsider)
        for name in ('problem_data', 'problem_media'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[problem.code])).status_code, 404)

    def test_data_screen_only_offers_the_package_upload(self):
        problem = self.make_problem()

        response = self.client.get(reverse('problem_data', args=[problem.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="package"')
        self.assertNotContains(response, 'name="problem-data-initfile"')
        self.assertNotContains(response, 'name="problem-data-zipfile"')
