import mimetypes
import os
from urllib.parse import quote, urljoin

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import DetailView

from judge.highlight_code import highlight_code
from judge.models import Problem, ProblemTestCase, Submission, problem_data_storage
from judge.utils.problem_package import PackageError, format_size, publish_package
from judge.utils.problem_propagation import propagate_problem_change
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin, add_file_response
from judge.views.problem import ProblemMixin

mimetypes.init()
mimetypes.add_type('application/x-yaml', '.yml')


class ProblemPackageForm(forms.Form):
    """Subida del paquete completo: la única forma de escribir un problema."""

    package = forms.FileField(
        label=_('problem package'),
        help_text=_('Zip with problem.yml, init.yml, the case archive, statement.md and optional media/.'),
    )

    def clean_package(self):
        package = self.cleaned_data['package']
        if not package.name.lower().endswith('.zip'):
            raise ValidationError(_("The package must be a .zip file"))
        max_size = getattr(settings, 'DMOJ_PROBLEM_PACKAGE_MAX_SIZE', None)
        if max_size is not None and package.size > max_size:
            raise ValidationError(
                _('The package is too big (max %s)') % format_size(max_size),
            )
        return package


def problem_media_url_base():
    """URL pública de la raíz de medios de problemas, o ``None`` si no se puede deducir.

    Los archivos los sirve el servidor web bajo ``MEDIA_URL``; la raíz de medios
    de problemas es un subdirectorio de ``MEDIA_ROOT`` en todos los entornos.
    """
    media_root = getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
    root = getattr(settings, 'MEDIA_ROOT', None)
    media_url = getattr(settings, 'MEDIA_URL', None)
    if not media_root or not root or not media_url:
        return None
    try:
        relative = os.path.relpath(media_root, root)
    except ValueError:
        return None
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    relative = relative.replace(os.sep, '/').strip('/')
    if not media_url.endswith('/'):
        media_url += '/'
    return urljoin(media_url, relative + '/') if relative else media_url


def list_problem_media(problem):
    """Archivos de medios del problema, en orden, con su URL servida."""
    media_root = getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
    base = problem_media_url_base()
    if not media_root or not base:
        return None

    directory = os.path.join(media_root, problem.code)
    if not os.path.isdir(directory):
        return []

    files = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            relative = os.path.relpath(path, directory).replace(os.sep, '/')
            files.append({
                'name': relative,
                'size': os.path.getsize(path),
                'url': base + quote('%s/%s' % (problem.code, relative)),
            })
    files.sort(key=lambda entry: entry['name'])
    return files


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

    def get_has_init(self):
        return problem_data_storage.exists(os.path.join(self.object.code, 'init.yml'))

    def get_all_cases(self):
        return ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by('order')

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        if 'form' not in context:
            context['form'] = ProblemPackageForm()
        context['all_cases'] = self.get_all_cases()
        context['has_init'] = self.get_has_init()
        return context

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        form = ProblemPackageForm(request.POST, request.FILES)
        result = None
        if form.is_valid():
            try:
                # El servicio valida y escribe: el formulario no arma ni toca
                # archivos por su cuenta.
                result = publish_package(form.cleaned_data['package'], problem=problem,
                                         author=request.user.profile, user=request.user)
            except PackageError as error:
                form.add_error('package', error)
        context = self.get_context_data(form=form)
        if result is not None:
            context['result'] = result
            # El paquete ya está commiteado; los jueces tienen que reescanear
            # igual que después de publicar por la API (ADR 0008).
            propagate_problem_change(result.problem_id)
        return self.render_to_response(context)


class ProblemMediaView(TitleMixin, ProblemManagerMixin):
    template_name = 'problem/media.html'

    def get_title(self):
        return _('Media for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Media for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.object.name,
                        reverse('problem_detail', args=[self.object.code]))))

    def get_context_data(self, **kwargs):
        context = super(ProblemMediaView, self).get_context_data(**kwargs)
        context['media_files'] = list_problem_media(self.object)
        context['media_root'] = getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
        return context


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
