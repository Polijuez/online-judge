import io
import uuid
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from judge.models import ProblemData
from judge.models.tests.util import create_problem
from judge.views.problem_data import ProblemDataForm

INIT_YML = b'test_cases:\n- batched:\n  - in: 1.in\n    out: 1.out\n  points: 100\n'


def make_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('1.in', '1\n')
        archive.writestr('1.out', '1\n')
    return buffer.getvalue()


class ProblemDataFormTestCase(TestCase):
    def setUp(self):
        self.problem = create_problem(code=str(uuid.uuid4()))
        self.data = ProblemData.objects.get_or_create(problem=self.problem)[0]

    def build_form(self, initfile_name, initfile_content):
        return ProblemDataForm(
            data={'problem-data-signature_grading': ''},
            files={
                'problem-data-initfile': SimpleUploadedFile(
                    initfile_name, initfile_content, content_type='application/x-yaml',
                ),
                'problem-data-zipfile': SimpleUploadedFile(
                    'data.zip', make_zip(), content_type='application/zip',
                ),
            },
            instance=self.data,
            prefix='problem-data',
        )

    def test_initfile_is_accepted_without_a_size_limit(self):
        # DMOJ_PROBLEM_MAX_FILE_SIZE is unset by default, so no size is enforced.
        form = self.build_form('init.yml', INIT_YML)
        self.assertTrue(form.is_valid(), form.errors)

    def test_initfile_with_wrong_extension_is_rejected(self):
        form = self.build_form('init.txt', INIT_YML)
        self.assertFalse(form.is_valid())
        self.assertIn('initfile', form.errors)

    def test_initfile_that_is_not_a_yaml_dictionary_is_rejected(self):
        form = self.build_form('init.yml', b'- not a dictionary\n')
        self.assertFalse(form.is_valid())
        self.assertIn('must be a YAML dictionary', str(form.errors))

    @override_settings(DMOJ_PROBLEM_MAX_FILE_SIZE=1)
    def test_initfile_over_the_size_limit_is_rejected(self):
        form = self.build_form('init.yml', INIT_YML)
        self.assertFalse(form.is_valid())
        self.assertIn('is too big (max 1B)', str(form.errors))
