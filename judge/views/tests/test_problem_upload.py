import os
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Problem
from judge.models.tests.util import create_user
from judge.utils.tests.fake_judge import FakeJudgeControlServer
from judge.views.tests.test_problem_data import make_package


class ProblemUploadViewTestCase(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='problem-upload-view-')
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
        self.client.force_login(self.user)

    # -- ayudas -----------------------------------------------------------

    def upload(self, raw, name='package.zip'):
        return self.client.post(reverse('problem_upload'), {
            'package': SimpleUploadedFile(name, raw, content_type='application/zip'),
        })

    def assertNothingWritten(self):
        self.assertEqual(Problem.objects.count(), 0)
        self.assertFalse(os.path.exists(self.data_root))
        self.assertFalse(os.path.exists(self.media_root))

    # -- la pantalla -------------------------------------------------------

    def test_the_page_offers_the_same_package_form(self):
        response = self.client.get(reverse('problem_upload'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="package"')
        self.assertContains(response, 'type="file"')

    def test_only_permitted_users_get_the_page_or_the_tab(self):
        self.client.logout()

        response = self.client.get(reverse('problem_upload'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])
        self.assertNotContains(self.client.get(reverse('problem_list')), reverse('problem_upload'))

        outsider = create_user(username='lurker')
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse('problem_upload')).status_code, 404)
        self.assertNotContains(self.client.get(reverse('problem_list')), reverse('problem_upload'))

        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('problem_list')), reverse('problem_upload'))

    # -- alta --------------------------------------------------------------

    def test_a_valid_package_creates_the_problem_and_shows_the_summary(self):
        response = self.upload(make_package(media={'figura.png': b'png'}))

        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get()
        self.assertRegex(problem.code, r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        self.assertEqual(problem.name, 'Suma de dos')
        self.assertTrue(problem.is_public)
        self.assertEqual(problem.description, '# Enunciado\n')
        self.assertEqual(problem.cases.count(), 1)
        self.assertEqual(
            list(problem.cases.values_list('order', 'input_file', 'output_file')),
            [(1, '1.in', '1.out')],
        )
        self.assertEqual(problem.data_files.initfile, '%s/init.yml' % problem.code)
        self.assertTrue(problem.authors.filter(id=self.user.profile.id).exists())

        self.assertTrue(os.path.isfile(os.path.join(self.data_root, problem.code, 'init.yml')))
        self.assertTrue(os.path.isfile(os.path.join(self.data_root, problem.code, 'data.zip')))
        self.assertTrue(os.path.isfile(os.path.join(self.media_root, problem.code, 'figura.png')))

        self.assertContains(response, problem.code)
        self.assertContains(response, 'data.zip')
        self.assertContains(response, 'created')
        self.assertContains(response, reverse('problem_detail', args=[problem.code]))
        self.assertContains(response, reverse('problem_data', args=[problem.code]))

    def test_uploading_warns_the_judges_like_the_api(self):
        judge = FakeJudgeControlServer()
        self.addCleanup(judge.close)

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url]):
            response = self.upload(make_package())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(judge.requests, ['/update/problems'])

    # -- rechazos ----------------------------------------------------------

    def test_an_invalid_package_shows_the_reason_and_writes_nothing(self):
        response = self.upload(make_package(omit=('init.yml',)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'missing required file')
        self.assertContains(response, 'init.yml')
        self.assertNothingWritten()

    def test_a_file_that_is_not_a_zip_is_rejected_by_the_form(self):
        response = self.upload(b'not a zip at all')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid zip file')
        self.assertNothingWritten()

    def test_publishing_public_without_the_permission_shows_the_reason(self):
        plain = create_user(username='plain', user_permissions=('edit_own_problem',))
        self.client.force_login(plain)

        response = self.upload(make_package())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'change_public_visibility')
        self.assertNothingWritten()
