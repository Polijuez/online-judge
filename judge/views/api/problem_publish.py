"""API de publicación de problemas.

``POST /api/v1/problems`` da de alta un problema publicando el paquete que va
en el cuerpo del request y ``PUT /api/v1/problems/<id>`` reemplaza el paquete
vigente de un problema existente. El cuerpo es el zip del paquete: el
manifiesto ``problem.yml`` viaja adentro.

La autenticación es el token Bearer por usuario que ya resuelve
``judge.middleware.APIMiddleware`` (acá solo se exige que haya pasado). La
autorización reutiliza las reglas del sitio: ``judge.edit_own_problem`` para el
alta y ``Problem.is_editable_by`` —autores, curadores y administradores de la
organización dueña— para el reemplazo, con el superusuario exento. Publicar un
manifiesto con ``visibility: public`` exige además
``judge.change_public_visibility``, igual que el admin; el chequeo lo hace el
servicio, así que el formulario de staff aplica la misma regla.

La validación y la escritura de problemas son del servicio
``judge.utils.problem_package``; esta vista solo traduce su resultado y sus
errores a HTTP y aplica los límites del request antes de llamarlo. Cuando la
escritura termina, ``judge.utils.problem_propagation`` pide a los jueces que
reescaneen su árbol; ese aviso es best-effort y no altera la respuesta.
"""

import logging

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from judge.models import Problem
from judge.utils.problem_package import PackageError, format_size, publish_package
from judge.utils.problem_propagation import propagate_problem_change

logger = logging.getLogger(__name__)

# Códigos del servicio que no son un paquete inválido.
_ERROR_STATUS = {
    'unsupported_schema': 422,
    # El servicio también cuenta archivos (el formulario de staff comparte el
    # mismo límite); es un request demasiado grande, no un paquete mal formado.
    'too_many_files': 413,
    'forbidden': 403,
}


class RequestLimitError(Exception):
    """El request excede un límite que se aplica antes de escribir nada."""

    def __init__(self, code, reason, status=413):
        self.code = code
        self.reason = reason
        self.status = status
        super().__init__(reason)


def error_response(code, reason, file=None, status=400):
    """Cuerpo de error uniforme: el archivo/field culpable y el motivo."""
    return JsonResponse({'error': {'code': code, 'file': file, 'reason': reason}}, status=status)


def publish_error_response(error):
    return error_response(error.code, error.reason, file=error.file,
                          status=_ERROR_STATUS.get(error.code, 400))


def declared_length(request):
    """Tamaño anunciado del cuerpo, o ``None`` si el request no lo declara."""
    try:
        return int(request.META.get('CONTENT_LENGTH') or 0) or None
    except (TypeError, ValueError):
        return None


def read_package(request):
    """Lee el zip del cuerpo respetando los límites antes de publicar.

    El tamaño declarado se compara antes de leer; el real después. La cantidad
    de archivos la cuenta el servicio, que es el camino compartido con el
    formulario de staff. Nada de esto toca disco ni base: el paquete llega al
    servicio solo si pasa los límites.
    """
    max_size = getattr(settings, 'DMOJ_PROBLEM_PACKAGE_MAX_SIZE', None)
    declared = declared_length(request)
    if max_size is not None and declared is not None and declared > max_size:
        raise RequestLimitError(
            'package_too_large',
            'the request body is %s, over the %s limit' % (format_size(declared), format_size(max_size)),
        )

    # Se lee a lo sumo un byte de más: alcanza para detectar el exceso sin
    # cargar un cuerpo enorme. No se usa ``request.body`` para no depender de
    # ``DATA_UPLOAD_MAX_MEMORY_SIZE``, que es global al sitio.
    raw = request.read() if max_size is None else request.read(max_size + 1)

    if not raw:
        raise RequestLimitError(
            'empty_package', 'the request body must be the problem package zip', status=400,
        )
    if max_size is not None and len(raw) > max_size:
        raise RequestLimitError(
            'package_too_large',
            'the request body is %s, over the %s limit' % (format_size(len(raw)), format_size(max_size)),
        )

    return raw


def publish_response(result, status):
    """Resumen de la publicación: qué quedó escrito y qué cambió."""
    payload = {
        'problem_id': result.problem_id,
        'url': reverse('problem_detail', args=[result.problem_id]),
        'created': result.created,
        'changed': result.changed,
        'files_written': list(result.files_written),
        'media_files': list(result.media_files),
        'cases': result.cases,
        'aliases_added': list(result.aliases_added),
        'warnings': list(result.warnings),
    }
    response = JsonResponse(payload, status=status)
    if status == 201:
        response['Location'] = payload['url']
    return response


@method_decorator(csrf_exempt, name='dispatch')
class BasePublishView(View):
    """Autenticación, errores JSON y logging comunes a las dos operaciones."""

    def dispatch(self, request, *args, **kwargs):
        try:
            response = self.authenticate(request) or super().dispatch(request, *args, **kwargs)
        except RequestLimitError as error:
            response = error_response(error.code, error.reason, status=error.status)
        except PackageError as error:
            if error.code == 'publish_failed':
                # Falla del servidor, no del paquete: el motivo va al log, no
                # al cliente (nombra rutas del disco).
                logger.exception('problem publish failed: %s', error.reason)
                response = error_response(error.code, 'the package could not be written', status=500)
            else:
                response = publish_error_response(error)
        # Solo método, ruta, usuario y estado: el token y el cuerpo nunca se loguean.
        logger.info('problem publish: %s %s user=%s status=%d', request.method, request.path,
                    getattr(request.user, 'pk', None), response.status_code)
        return response

    def authenticate(self, request):
        # El token Bearer es la única credencial del endpoint: sin él no hay
        # usuario ni nada que autorizar. ``APIMiddleware`` ya verificó el HMAC.
        if not request.headers.get('authorization') or not request.user.is_authenticated:
            response = error_response('unauthorized', 'a Bearer API token is required', status=401)
            response['WWW-Authenticate'] = 'Bearer realm="API"'
            return response
        return None

    def http_method_not_allowed(self, request, *args, **kwargs):
        response = error_response('method_not_allowed', 'method %s is not allowed' % request.method, status=405)
        response['Allow'] = ', '.join(self._allowed_methods())
        return response


class ProblemPublishView(BasePublishView):
    """Alta de un problema: crea y publica en una sola operación."""

    http_method_names = ['post']

    def post(self, request):
        user = request.user
        if not (user.is_superuser or user.has_perm('judge.edit_own_problem')):
            return error_response('forbidden', 'you cannot create problems', status=403)

        raw = read_package(request)
        result = publish_package(raw, author=request.profile, user=user)
        propagate_problem_change(result.problem_id)
        return publish_response(result, status=201)


class ProblemReplaceView(BasePublishView):
    """Reemplazo del paquete vigente de un problema existente."""

    http_method_names = ['put']

    def put(self, request, problem_id):
        problem = Problem.objects.filter(code=problem_id).first()
        if problem is None:
            return error_response('not_found', 'no problem with id %r' % problem_id, status=404)

        user = request.user
        if not (user.is_superuser or problem.is_editable_by(user)):
            return error_response('forbidden', 'you cannot edit problem %s' % problem_id, status=403)

        raw = read_package(request)
        result = publish_package(raw, problem=problem, user=user)
        propagate_problem_change(result.problem_id)
        return publish_response(result, status=200)
