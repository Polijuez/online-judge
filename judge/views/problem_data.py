import json
import yaml
import mimetypes
import os
from itertools import chain
from typing import List
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.forms import BaseModelFormSet, HiddenInput, ModelForm, NumberInput, Select, formset_factory
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import DetailView
from django.db import transaction

from judge.highlight_code import highlight_code
from judge.models import Problem, ProblemData, ProblemTestCase, Submission, problem_data_storage, \
    problem_directory_file
from judge.utils.problem_package import ProblemDataLoader
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin, add_file_response
from judge.views.problem import ProblemMixin

mimetypes.init()
mimetypes.add_type('application/x-yaml', '.yml')

FIELD_CLEAR_MARKER = False ## value used in forms to clear a field

def _read_yml(upload) -> dict | str:
    try:
        content = upload.read()
        # Validar que sea YAML parseable
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            return _("%s must be a YAML dictionary") % upload.name
    except yaml.YAMLError as exc:
        return _("invalid YAML: %s") % exc
    finally:
        # Rebobinar el puntero para que Django pueda guardarlo en disco
        upload.seek(0)
    return parsed

def _format_file_size(size):
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}KB"
    return f"{size // 1024 // 1024}MB"

def _check_file_size(file, max_size):
    if max_size is not None and file.size > max_size:
        raise ValidationError(_("%s is too big (max %s)") % (file.name, _format_file_size(max_size)))

def check_problem_file(file, ext=None):
    """Validate a problem data file: its extension and, when configured, its size."""
    if ext and not file.name.endswith(ext):
        if isinstance(ext, str):
            raise ValidationError(_("%s must be a %s file") % (file.name, ext))
        else:
            raise ValidationError(_("%s format is not accepted") % file.name)
    _check_file_size(file, getattr(settings, 'DMOJ_PROBLEM_MAX_FILE_SIZE', None))

def _get_valid_name(data, filename):
    return problem_directory_file(data, problem_data_storage.get_valid_name(filename))

def check_file_duplicate(data, upload:list[str], stored:list[str]):
    filenames = []
    for upd,in_store in zip(upload,stored):
        if upd is False:
            continue
        if upd:
            filenames.append(upd)
        elif in_store:
            filenames.append(in_store)

    unique = set()
    for file in filenames:
        if file in unique:
            raise ValidationError(_("%s is duplicated!") % file)
        unique.add(file)


class ProblemDataForm(ModelForm):
    def clean_zipfile(self):
        if hasattr(self, 'zip_valid') and not self.zip_valid:
            raise ValidationError(_('Your zip file is invalid!'))

        zipfile = self.cleaned_data['zipfile']
        if zipfile and not zipfile.name.endswith('.zip'):
            raise ValidationError(_("Zip files must end in '.zip'"))

        return zipfile

    def clean_initfile(self):
        initfile = self.cleaned_data['initfile']
        if not initfile:
            return initfile

        check_problem_file(initfile, ('.yml', '.yaml'))
        ## Inspeccionar contenido de forma segura
        self.inityml = _read_yml(initfile)
        if not isinstance(self.inityml,dict):
            raise ValidationError(self.inityml)

        return initfile

    def clean_checker(self):
        checker = self.cleaned_data['checker']
        if not checker:
            return checker
        check_problem_file(checker, ".py")
        return checker

    def clean_grader(self):
        grader = self.cleaned_data['grader']
        if not grader:
            return grader
        check_problem_file(grader, ".py")
        return grader

    def clean_signature(self):
        signature = self.cleaned_data['signature']
        if not signature:
            return signature
        check_problem_file(signature, ".hpp")
        return signature

    def clean_evaluator(self):
        evaluator = self.cleaned_data['evaluator']
        if not evaluator:
            return evaluator
        check_problem_file(evaluator, ".cpp")
        return evaluator

    def clean(self):
        if isinstance(getattr(self, 'inityml', None), dict):
            valid_files = True  # defaults to all files
            if hasattr(self, 'valid_files'):
                valid_files = self.valid_files
            loader = ProblemDataLoader(
                self.instance.problem,
                self.inityml,
                valid_files,
                dry_run=True
            )
            loader.load()

        signature_grading = self.cleaned_data['signature_grading']
        signature = self.cleaned_data.get('signature',None)
        evaluator = self.cleaned_data.get('evaluator',None)
        if not signature_grading:
            self.cleaned_data['signature'] = FIELD_CLEAR_MARKER
            self.cleaned_data['evaluator'] = FIELD_CLEAR_MARKER
        elif not signature or not evaluator:
            raise ValidationError(_("Signature and evaluator are required"))

        file_fields = ProblemData.get_file_fields()
        file_originals = ProblemData.get_file_originals()
        upload = []
        for file,original in zip(file_fields,file_originals):
            if self.cleaned_data.get(file):
                if getattr(self.instance,original)!=self.cleaned_data.get(file):
                    upload.append(_get_valid_name(self.instance,self.cleaned_data.get(file).name))
                else:
                    upload.append(self.cleaned_data.get(file).name)
            else:
                upload.append(self.cleaned_data.get(file))
        stored = [getattr(self.instance,file).name for file in file_fields]
        check_file_duplicate(self.instance,upload,stored)

        return self.cleaned_data

    class Meta:
        model = ProblemData
        fields = ['zipfile', 'initfile', 'checker', 'grader', 'signature_grading', 'signature', 'evaluator']

class ProblemCaseForm(ModelForm):
    class Meta:
        model = ProblemTestCase
        fields = ('order', 'type', 'input_file', 'output_file', 'points')
        widgets = {
            'type': Select(attrs={'style': 'width: 100%'}),
            'points': NumberInput(attrs={'style': 'width: 4em'}),
        }

class ProblemManagerMixin(LoginRequiredMixin, ProblemMixin, DetailView):
    def get_object(self, queryset=None):
        problem = super(ProblemManagerMixin, self).get_object(queryset)
        if problem.is_manually_managed:
            raise Http404()
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

class ProblemSubmissionDiff(TitleMixin, ProblemMixin, DetailView):
    template_name = 'problem/submission-diff.html'

    def get_title(self):
        return _('Comparing submissions for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Comparing submissions for {0}')).format(
            format_html('<a href="{1}">{0}</a>', self.object.name, reverse('problem_detail', args=[self.object.code])),
        ))

    def get_object(self, queryset=None):
        problem = super(ProblemSubmissionDiff, self).get_object(queryset)
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionDiff, self).get_context_data(**kwargs)
        try:
            ids = self.request.GET.getlist('id')
            subs = Submission.objects.filter(id__in=ids)
        except ValueError:
            raise Http404
        if not subs:
            raise Http404

        context['submissions'] = subs

        # If we have associated data we can do better than just guess
        data = ProblemTestCase.objects.filter(dataset=self.object, type='C')
        if data:
            num_cases = data.count()
        else:
            num_cases = subs.first().test_cases.count()
        context['num_cases'] = num_cases
        return context


class ProblemDataView(TitleMixin, ProblemManagerMixin):
    template_name = 'problem/data.html'

    def get_title(self):
        return _('Editing data for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Editing data for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.object.name,
                        reverse('problem_detail', args=[self.object.code]))))

    def get_data_form(self, post=False):
        return ProblemDataForm(data=self.request.POST if post else None, prefix='problem-data',
                               files=self.request.FILES if post else None,
                               instance=ProblemData.objects.get_or_create(problem=self.object)[0])

    def get_all_cases(self):
        return ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by('order')

    def get_valid_files(self, data, post=False) -> List[str]:
        try:
            if post and 'problem-data-zipfile-clear' in self.request.POST:
                return []
            elif post and 'problem-data-zipfile' in self.request.FILES:
                return ZipFile(self.request.FILES['problem-data-zipfile']).namelist()
            elif data.zipfile:
                return ZipFile(data.zipfile.path).namelist()
        except BadZipfile:
            raise
        return []

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        if 'data_form' not in context:
            context['data_form'] = self.get_data_form()
        context['all_cases'] = self.get_all_cases()
        return context

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        data_form = self.get_data_form(post=True)
        try:
            valid_files = self.get_valid_files(data_form.instance, post=True)
            data_form.zip_valid = True
            data_form.valid_files = valid_files
        except BadZipfile:
            valid_files = []
            data_form.zip_valid = False

        if data_form.is_valid():
            inityml = _read_yml(data_form.cleaned_data['initfile'])
            cases = None
            if isinstance(inityml,dict):
                loader = ProblemDataLoader(
                    problem,
                    inityml,
                    valid_files,
                    dry_run=False
                )
                cases = loader.load()
            with transaction.atomic():
                data_form.save()
                # Delete all previous cases
                problem.cases.all().delete()
                if cases:
                    ProblemTestCase.objects.bulk_create(cases)
            return HttpResponseRedirect(request.get_full_path())
        
        return self.render_to_response(self.get_context_data(data_form=data_form))

    put = post


@login_required
def problem_data_file(request, problem, path):
    object = get_object_or_404(Problem, code=problem)
    if not object.is_editable_by(request.user):
        raise Http404()

    problem_dir = problem_data_storage.path(problem)
    if os.path.commonpath((problem_data_storage.path(os.path.join(problem, path)), problem_dir)) != problem_dir:
        raise Http404()

    response = HttpResponse()

    if hasattr(settings, 'DMOJ_PROBLEM_DATA_INTERNAL'):
        url_path = '%s/%s/%s' % (settings.DMOJ_PROBLEM_DATA_INTERNAL, problem, path)
    else:
        url_path = None

    try:
        add_file_response(request, response, url_path, os.path.join(problem, path), problem_data_storage)
    except IOError:
        raise Http404()

    response['Content-Type'] = 'application/octet-stream'
    return response

@login_required
def problem_init_view(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    if not problem.is_editable_by(request.user):
        raise Http404()

    try:
        with problem_data_storage.open(os.path.join(problem.code, 'init.yml'), 'rb') as f:
            data = utf8text(f.read()).rstrip('\n')
    except IOError:
        raise Http404()

    return render(request, 'problem/yaml.html', {
        'raw_source': data, 'highlighted_source': highlight_code(data, 'yaml'),
        'title': _('Generated init.yml for %s') % problem.name,
        'content_title': mark_safe(escape(_('Generated init.yml for %s')) % (
            format_html('<a href="{1}">{0}</a>', problem.name,
                        reverse('problem_detail', args=[problem.code])))),
    })
