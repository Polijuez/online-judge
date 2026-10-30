import io
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile

import yaml

from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Problem
from judge.models.tests.util import create_organization, create_problem, create_user
from judge.utils.tests.fake_judge import FakeJudgeControlServer, closed_port_endpoint

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


def yaml_dump(data):
    return yaml.safe_dump(data).encode('utf-8')


def make_package(manifest=None, init=INIT_YML, statement='# Enunciado\n', cases=None, media=None, omit=(),
                 extra=None):
    data = dict(MANIFEST)
    data.update(manifest or {})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        if 'problem.yml' not in omit:
            archive.writestr('problem.yml', yaml_dump(data))
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
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
        for name, content in (media or {}).items():
            archive.writestr('media/' + name, content)
    return buffer.getvalue()


class ProblemPublishAPITestCase(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='problem-publish-api-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = os.path.join(self.tmp, 'data')
        self.media_root = os.path.join(self.tmp, 'media', 'problems')
        self.settings = override_settings(
            DMOJ_PROBLEM_DATA_ROOT=self.data_root,
            DMOJ_PROBLEM_MEDIA_ROOT=self.media_root,
            DMOJ_PROBLEM_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE=None,
            DMOJ_PROBLEM_MEDIA_MAX_FILES=None,
            # Ningún test habla con los jueces reales: los de propagación
            # configuran su servidor falso.
            DMOJ_JUDGE_CONTROL_ENDPOINTS=[],
            DMOJ_JUDGE_CONTROL_TIMEOUT=0.5,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    # -- ayudas -----------------------------------------------------------

    def user_with_token(self, username, permissions=()):
        user = create_user(username=username, user_permissions=permissions)
        return user, 'Bearer ' + user.profile.generate_api_token()

    def post_package(self, raw, token=None):
        headers = {'HTTP_AUTHORIZATION': token} if token else {}
        return self.client.post(reverse('api_problem_publish'), data=raw,
                                content_type='application/zip', **headers)

    def put_package(self, problem_id, raw, token=None):
        headers = {'HTTP_AUTHORIZATION': token} if token else {}
        return self.client.put(reverse('api_problem_replace', args=[problem_id]), data=raw,
                               content_type='application/zip', **headers)

    def body(self, response):
        return json.loads(response.content.decode('utf-8'))

    def assertNothingWritten(self):
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))
        self.assertFalse(os.path.exists(self.media_root))

    # -- alta -------------------------------------------------------------

    def test_create_returns_201_with_the_uuid_and_what_was_written(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package(media={'figura.png': b'png'})

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 201)
        payload = self.body(response)
        problem = Problem.objects.get(code=payload['problem_id'])
        self.assertRegex(problem.code, r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        self.assertEqual(payload['problem_id'], problem.code)
        self.assertEqual(response['Location'], '/problem/%s' % problem.code)
        self.assertEqual(payload['url'], response['Location'])
        self.assertTrue(payload['created'])
        self.assertTrue(payload['changed'])
        self.assertEqual(payload['files_written'], ['data.zip', 'init.yml'])
        self.assertEqual(payload['media_files'], ['media/figura.png', 'media/statement.md'])
        self.assertEqual(payload['cases'], 1)
        self.assertEqual(payload['aliases_added'], ['suma-de-dos', 'suma'])

        # El problema queda calificable: metadata, límites y casos aplicados.
        self.assertEqual(problem.name, 'Suma de dos')
        self.assertTrue(problem.is_public)
        self.assertEqual(problem.cases.count(), 1)
        self.assertEqual(problem.types.values_list('name', flat=True).get(), 'math')
        self.assertTrue(problem.allowed_languages.exists())
        self.assertEqual(problem.data_files.initfile, '%s/init.yml' % problem.code)
        self.assertEqual(problem.data_files.zipfile, '%s/data.zip' % problem.code)
        self.assertTrue(os.path.isfile(os.path.join(self.data_root, problem.code, 'init.yml')))
        self.assertTrue(os.path.isfile(os.path.join(self.data_root, problem.code, 'data.zip')))
        self.assertTrue(os.path.isfile(os.path.join(self.media_root, problem.code, 'figura.png')))

        # El que publica queda como autor, así puede reemplazar después.
        self.assertTrue(problem.authors.filter(id=user.profile.id).exists())

    def test_superuser_can_create_without_explicit_permission(self):
        user, token = self.user_with_token('root')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Problem.objects.count(), 1)

    # -- reemplazo --------------------------------------------------------

    def test_replace_returns_200_with_the_change_summary(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        created = self.body(self.post_package(make_package(), token))

        raw = make_package(manifest={'title': 'Suma de dos (v2)', 'aliases': ['suma-de-dos', 'suma-v2']})
        response = self.put_package(created['problem_id'], raw, token)

        self.assertEqual(response.status_code, 200)
        payload = self.body(response)
        self.assertEqual(payload['problem_id'], created['problem_id'])
        self.assertFalse(payload['created'])
        self.assertTrue(payload['changed'])
        self.assertEqual(payload['aliases_added'], ['suma-v2'])
        problem = Problem.objects.get(code=created['problem_id'])
        self.assertEqual(problem.name, 'Suma de dos (v2)')
        self.assertTrue(problem.aliases.filter(name='suma-v2').exists())

    def test_replace_is_idempotent(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        created = self.body(self.post_package(make_package(), token))
        raw = make_package(manifest={'title': 'Suma de dos v2'})

        first = self.put_package(created['problem_id'], raw, token)
        second = self.put_package(created['problem_id'], raw, token)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(self.body(first)['changed'])
        self.assertFalse(self.body(second)['changed'])
        self.assertEqual(self.body(second)['aliases_added'], [])
        self.assertEqual(Problem.objects.get(code=created['problem_id']).name, 'Suma de dos v2')

    def test_replace_of_a_missing_problem_is_404(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        response = self.put_package(str(uuid.uuid4()), make_package(), token)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.body(response)['error']['code'], 'not_found')

    def test_replace_with_a_non_uuid_id_is_404(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        response = self.put_package('not-a-uuid', make_package(), token)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.body(response)['error']['code'], 'not_found')

    # -- autenticación y autorización -------------------------------------

    def test_request_without_token_gets_401_with_www_authenticate(self):
        response = self.post_package(make_package())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['WWW-Authenticate'], 'Bearer realm="API"')
        self.assertEqual(self.body(response)['error']['code'], 'unauthorized')
        self.assertNothingWritten()

    def test_session_without_a_token_gets_401(self):
        user = create_user(username='problemsetter', user_permissions=('edit_own_problem', 'change_public_visibility'))
        self.client.force_login(user)

        response = self.post_package(make_package())

        self.assertEqual(response.status_code, 401)
        self.assertNothingWritten()

    def test_invalid_token_gets_401(self):
        user = create_user(username='problemsetter', user_permissions=('edit_own_problem', 'change_public_visibility'))
        token = 'Bearer ' + 'A' * 48

        response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 401)
        self.assertNothingWritten()

    def test_user_without_permission_gets_403(self):
        user, token = self.user_with_token('lurker')

        response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)['error']['code'], 'forbidden')
        self.assertNothingWritten()

    def test_replace_of_a_problem_you_do_not_edit_gets_403(self):
        owner, owner_token = self.user_with_token('owner', ('edit_own_problem', 'change_public_visibility'))
        created = self.body(self.post_package(make_package(), owner_token))
        other, other_token = self.user_with_token('other', ('edit_own_problem', 'change_public_visibility'))

        response = self.put_package(created['problem_id'], make_package(), other_token)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)['error']['code'], 'forbidden')
        self.assertEqual(Problem.objects.get(code=created['problem_id']).name, 'Suma de dos')

    def test_organization_membership_decides_who_can_replace(self):
        create_organization(name='club')
        problem = create_problem(code=str(uuid.uuid4()), is_organization_private=True,
                                 organizations=['club'])
        outsider, outsider_token = self.user_with_token('outsider', ('edit_own_problem', 'change_public_visibility'))

        forbidden = self.put_package(problem.code, make_package(), outsider_token)

        self.assertEqual(forbidden.status_code, 403)
        self.assertFalse(problem.aliases.exists())

        admin, admin_token = self.user_with_token('club-admin', ('edit_own_problem', 'change_public_visibility'))
        problem.organizations.first().admins.add(admin.profile)

        allowed = self.put_package(problem.code, make_package(), admin_token)

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(self.body(allowed)['changed'])

    def test_public_publish_without_change_public_visibility_gets_403(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem',))

        response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 403)
        error = self.body(response)['error']
        self.assertEqual(error['code'], 'forbidden')
        self.assertEqual(error['file'], 'problem.yml')
        self.assertIn('change_public_visibility', error['reason'])
        self.assertNothingWritten()

    def test_replace_public_without_change_public_visibility_gets_403(self):
        user = create_user(username='problemsetter', user_permissions=('edit_own_problem',))
        problem = create_problem(code=str(uuid.uuid4()), authors=['problemsetter'])
        token = 'Bearer ' + user.profile.generate_api_token()

        response = self.put_package(problem.code, make_package(), token)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)['error']['code'], 'forbidden')
        problem.refresh_from_db()
        self.assertEqual(problem.name, problem.code)
        self.assertFalse(problem.aliases.exists())
        self.assertFalse(os.path.exists(self.data_root))

    def test_a_private_publish_needs_no_change_public_visibility(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem',))

        response = self.post_package(make_package(manifest={'visibility': 'private'}), token)

        self.assertEqual(response.status_code, 201)
        problem = Problem.objects.get(code=self.body(response)['problem_id'])
        self.assertFalse(problem.is_public)

    # -- errores de validación -------------------------------------------

    def test_unknown_manifest_schema_gets_422_with_the_reason(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        response = self.post_package(make_package(manifest={'schema': 2}), token)

        self.assertEqual(response.status_code, 422)
        error = self.body(response)['error']
        self.assertEqual(error['code'], 'unsupported_schema')
        self.assertEqual(error['file'], 'problem.yml')
        self.assertIn('unknown schema 2', error['reason'])
        self.assertNothingWritten()

    def test_a_manifest_without_schema_is_a_malformed_package(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package()
        # Saca el schema del manifiesto sin tocar el resto del zip.
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(buffer, 'w') as target:
            for info in source.infolist():
                content = source.read(info)
                if info.filename == 'problem.yml':
                    manifest = yaml.safe_load(content)
                    manifest.pop('schema')
                    content = yaml.safe_dump(manifest).encode('utf-8')
                target.writestr(info, content)

        response = self.post_package(buffer.getvalue(), token)

        self.assertEqual(response.status_code, 400)
        error = self.body(response)['error']
        self.assertEqual(error['file'], 'problem.yml')
        self.assertEqual(error['reason'], 'schema is required')
        self.assertNothingWritten()

    def test_invalid_package_names_the_offending_file(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package(omit=('init.yml',))

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 400)
        error = self.body(response)['error']
        self.assertEqual(error['file'], 'init.yml')
        self.assertIn('missing required file', error['reason'])
        self.assertNothingWritten()

    def test_a_non_string_case_file_gets_400_with_the_reason(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package(init=b'archive: data.zip\ntest_cases:\n- points: 100\n  in: [1.in]\n  out: 1.out\n')

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 400)
        error = self.body(response)['error']
        self.assertEqual(error['file'], 'init.yml')
        self.assertIn('must be file names', error['reason'])
        self.assertNothingWritten()

    def test_signature_without_evaluator_gets_400_with_the_reason(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package(extra={'signature.hpp': b'int firma();'})

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 400)
        error = self.body(response)['error']
        self.assertEqual(error['file'], 'signature.hpp')
        self.assertIn('evaluator', error['reason'])
        self.assertNothingWritten()

    def test_a_non_zip_body_is_rejected_without_writing(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        response = self.post_package(b'not a zip at all', token)

        self.assertEqual(response.status_code, 400)
        self.assertIn('not a valid zip file', self.body(response)['error']['reason'])
        self.assertNothingWritten()

    # -- límites del request ---------------------------------------------

    @override_settings(DMOJ_PROBLEM_PACKAGE_MAX_SIZE=100)
    def test_oversized_request_is_rejected_without_writing(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package()
        self.assertGreater(len(raw), 100)

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 413)
        error = self.body(response)['error']
        self.assertEqual(error['code'], 'package_too_large')
        self.assertIn('over the 100B limit', error['reason'])
        self.assertNothingWritten()

    @override_settings(DMOJ_PROBLEM_PACKAGE_MAX_FILES=2)
    def test_too_many_files_is_rejected_without_writing(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        raw = make_package(media={'figura.png': b'png'})

        response = self.post_package(raw, token)

        self.assertEqual(response.status_code, 413)
        error = self.body(response)['error']
        self.assertEqual(error['code'], 'too_many_files')
        self.assertIn('over the 2 limit', error['reason'])
        self.assertNothingWritten()

    def test_empty_body_is_rejected(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        response = self.post_package(b'', token)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)['error']['code'], 'empty_package')
        self.assertNothingWritten()

    # -- métodos ----------------------------------------------------------

    def test_other_methods_are_rejected(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        created = self.body(self.post_package(make_package(), token))
        headers = {'HTTP_AUTHORIZATION': token}

        publish_url = reverse('api_problem_publish')
        replace_url = reverse('api_problem_replace', args=[created['problem_id']])

        for method, url in (
            ('get', publish_url), ('put', publish_url), ('delete', publish_url),
            ('get', replace_url), ('post', replace_url), ('delete', replace_url),
        ):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, **headers)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(self.body(response)['error']['code'], 'method_not_allowed')
                self.assertTrue(response['Allow'])

    # -- logging ----------------------------------------------------------

    def test_the_token_is_not_logged_nor_echoed(self):
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        secret = token.split(' ', 1)[1]

        with self.assertLogs(level='INFO') as logs:
            response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 201)
        logged = '\n'.join(logs.output)
        self.assertNotIn(secret, logged)
        self.assertNotIn(secret, response.content.decode('utf-8'))

    # -- propagación a los jueces -----------------------------------------

    def test_publishing_warns_the_judges_to_rescan(self):
        judge = FakeJudgeControlServer()
        self.addCleanup(judge.close)
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url]):
            response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(judge.requests, ['/update/problems'])

    def test_replacing_warns_the_judges_to_rescan(self):
        judge = FakeJudgeControlServer()
        self.addCleanup(judge.close)
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))
        created = self.body(self.post_package(make_package(), token))

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url]):
            response = self.put_package(created['problem_id'], make_package(manifest={'title': 'Suma de dos v2'}),
                                        token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(judge.requests, ['/update/problems'])

    def test_a_down_judge_does_not_fail_the_publish_and_is_logged(self):
        endpoint = closed_port_endpoint()
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[endpoint]):
            with self.assertLogs('judge.problem_propagation', level='WARNING') as logs:
                response = self.post_package(make_package(), token)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Problem.objects.filter(code=self.body(response)['problem_id']).exists())
        self.assertIn(endpoint, '\n'.join(logs.output))

    def test_a_hanging_judge_does_not_delay_the_publish_beyond_the_timeout(self):
        judge = FakeJudgeControlServer(delay=5)
        self.addCleanup(judge.close)
        user, token = self.user_with_token('problemsetter', ('edit_own_problem', 'change_public_visibility'))

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url], DMOJ_JUDGE_CONTROL_TIMEOUT=0.3):
            with self.assertLogs('judge.problem_propagation', level='INFO'):
                started = time.monotonic()
                response = self.post_package(make_package(), token)
                elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 201)
        # El juez tarda 5s en contestar; la publicación no espera más que el timeout.
        self.assertLess(elapsed, 1.0)
