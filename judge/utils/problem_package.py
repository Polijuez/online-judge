"""Publicar un paquete de problema: validación, escritura atómica y metadata.

Un paquete es un zip con las entradas en la raíz:

* ``problem.yml`` (obligatorio): el manifiesto del sitio, ``schema: 1``.
* ``init.yml`` (obligatorio): lo que el juez consume; su clave ``archive:``
  nombra el zip de casos.
* el zip de casos nombrado por ``init.yml`` (obligatorio).
* ``statement.md`` o ``statement.pdf`` (exactamente uno): el enunciado. El
  markdown se publica tal cual con sus referencias relativas reescritas a la
  URL de medios del problema; el pdf se incrusta con ``<embed>``.
* ``checker.py``, ``grader.py``, ``signature.hpp`` y ``evaluator.cpp``
  (opcionales). También se aceptan los nombres que ``init.yml`` declare en
  ``checker``, ``grader``, ``signature``, ``evaluator`` o ``custom_judge``.
* ``media/...`` (opcional): archivos del enunciado.

El manifiesto tiene esta forma (``schema``, ``title``, ``group`` y
``visibility`` son obligatorias; ``limits`` y ``types`` pueden
omitirse y quedan vacías; las claves marcadas como opcionales solo pisan el
valor del problema cuando están)::

    schema: 1
    title: "Nombre del problema"
    limits:
      .cpp:
        tl: 1.0          # segundos
        ml: 262144       # KiB
    types: [math]
    group: basico
    visibility: public   # public | private
    time_limit: 2.0      # opcional; límite por defecto del problema
    memory_limit: 262144 # opcional; KiB
    points: 100          # opcional

Un ``schema`` distinto de ``1`` se rechaza sin intentar interpretarlo. Las
claves desconocidas también: el manifiesto es un contrato del servidor.

Los límites se aplican a **todas** las ``Language`` con esa extensión (ADR
0006). Publicar reemplaza el paquete vigente (ADR 0005): el árbol de datos y el
de medios se intercambian completos, así que lo que ya no está en el paquete
tampoco queda en disco. La escritura ocurre en directorios temporales de la
misma raíz y el intercambio es lo último; si algo falla antes, el paquete
anterior queda intacto.
"""
import io
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from judge.models import Language, LanguageLimit, Problem, ProblemData, ProblemGroup, ProblemTestCase, ProblemType
from judge.models.problem import PROBLEM_ID_REGEX

__all__ = [
    'PackageError', 'PublishResult', 'load_case_specs', 'publish_package',
    'STATEMENT_MD_NAME', 'STATEMENT_PDF_NAME',
]

MANIFEST_NAME = 'problem.yml'
INIT_NAME = 'init.yml'
STATEMENT_MD_NAME = 'statement.md'
STATEMENT_PDF_NAME = 'statement.pdf'
# Compatibilidad con el nombre histórico del enunciado en markdown.
STATEMENT_NAME = STATEMENT_MD_NAME
STATEMENT_NAMES = (STATEMENT_MD_NAME, STATEMENT_PDF_NAME)
MEDIA_PREFIX = 'media/'
SUPPORTED_SCHEMA = 1

#: Claves de ``init.yml`` que pueden nombrar un archivo del juez.
INIT_FILE_KEYS = ('checker', 'grader', 'signature', 'evaluator', 'custom_judge')

MANIFEST_KEYS = frozenset({
    'schema', 'title', 'limits', 'types', 'group', 'visibility',
    'time_limit', 'memory_limit', 'points',
})
LIMIT_KEYS = frozenset({'tl', 'ml'})
VISIBILITIES = {'public': True, 'private': False}

DEFAULT_TIME_LIMIT = 2.0
DEFAULT_MEMORY_LIMIT = 262144
DEFAULT_POINTS = 100

#: Formatos de medios aceptados cuando el despliegue no configura una lista.
DEFAULT_MEDIA_FORMATS = (
    # imágenes y documentos del enunciado
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.pdf', '.md',
    # archivos que el enunciado ofrece para descargar: kits y plantillas
    '.zip', '.hs', '.py', '.cpp', '.h', '.hpp', '.c', '.java', '.txt',
)

_EXTENSION_RE = re.compile(r'^[A-Za-z0-9_+-]{1,10}$')
_SAFE_ENTRY_RE = re.compile(r'^[^/\\\x00]+$')


class PackageError(Exception):
    """Error de validación o de publicación de un paquete.

    ``file`` nombra el archivo del paquete (o ``None`` si el problema es del
    paquete en conjunto) y ``reason`` explica el motivo. El mensaje incluye
    ambos para que un cliente HTTP pueda devolverlos tal cual.
    """

    def __init__(self, reason, file=None, code='invalid_package'):
        self.reason = reason
        self.file = file
        self.code = code
        super().__init__('%s: %s' % (file, reason) if file else reason)


@dataclass(frozen=True)
class PublishResult:
    """Lo que dejó una publicación."""

    problem_id: str
    files_written: tuple
    media_files: tuple
    cases: int
    warnings: tuple
    created: bool
    changed: bool


@dataclass
class _Manifest:
    title: str
    limits: dict
    types: tuple
    group: str
    is_public: bool
    time_limit: float = None
    memory_limit: int = None
    points: float = None


@dataclass
class _Package:
    manifest: _Manifest
    init_bytes: bytes
    init_yml: dict
    archive_name: str
    archive_bytes: bytes
    #: Nombre del archivo de enunciado que trae el paquete (md o pdf).
    statement_name: str
    #: Contenido crudo del archivo de enunciado, tal como vino en el zip.
    statement_bytes: bytes
    #: Texto del enunciado markdown decodificado (``None`` para pdf).
    statement_text: str = None
    optional: dict = field(default_factory=dict)
    file_fields: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)

    def data_files(self):
        files = {INIT_NAME: self.init_bytes, self.archive_name: self.archive_bytes}
        files.update(self.optional)
        return files


def _package_error(reason, file=None, code='invalid_package'):
    return PackageError(reason, file=file, code=code)


# ---------------------------------------------------------------------------
# Lectura y validación del zip
# ---------------------------------------------------------------------------

def _read_source(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if hasattr(source, 'read'):
        return source.read()
    with open(source, 'rb') as f:
        return f.read()


def _safe_entry_name(name):
    """Valida una entrada de zip y devuelve su forma POSIX normalizada."""
    if not name or name.startswith('/') or name.startswith('\\'):
        raise _package_error('path must be relative to the package root', file=name)
    if '\\' in name or '\x00' in name:
        raise _package_error('invalid path in package', file=name)
    parts = name.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise _package_error('invalid path in package', file=name)
    return '/'.join(parts)


def _is_symlink(info):
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _read_entries(raw):
    """Devuelve ``{nombre: bytes}`` de las entradas regulares del zip."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise _package_error('not a valid zip file (%s)' % exc, file=None)

    max_size = getattr(settings, 'DMOJ_PROBLEM_MAX_FILE_SIZE', None)
    max_files = getattr(settings, 'DMOJ_PROBLEM_PACKAGE_MAX_FILES', None)
    entries = {}
    try:
        if max_files is not None:
            count = sum(1 for info in archive.infolist() if not info.is_dir())
            if count > max_files:
                raise _package_error(
                    'the package has %d files, over the %d limit' % (count, max_files),
                    file=None, code='too_many_files',
                )
        for info in archive.infolist():
            name = info.filename
            if info.is_dir():
                continue
            name = _safe_entry_name(name)
            if name in entries:
                raise _package_error('duplicated entry in package', file=name)
            if info.flag_bits & 0x1:
                raise _package_error('encrypted entries are not accepted', file=name)
            if _is_symlink(info):
                raise _package_error('symlinks are not accepted', file=name)
            if max_size is not None and info.file_size > max_size:
                raise _package_error('file is too big (max %s)' % format_size(max_size), file=name)
            entries[name] = archive.read(info)
    finally:
        archive.close()
    return entries


def _read_nested_archive(name, raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise _package_error('not a valid zip file (%s)' % exc, file=name)

    max_size = getattr(settings, 'DMOJ_PROBLEM_MAX_FILE_SIZE', None)
    names = {}
    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            entry = _safe_entry_name(info.filename)
            if entry in names:
                raise _package_error('duplicated entry in case archive', file=name)
            if info.flag_bits & 0x1:
                raise _package_error('encrypted entries are not accepted', file=name)
            if _is_symlink(info):
                raise _package_error('symlinks are not accepted', file=name)
            if max_size is not None and info.file_size > max_size:
                raise _package_error(
                    'file is too big (max %s)' % format_size(max_size), file=name,
                )
            names[entry] = info.file_size
    finally:
        archive.close()
    return names


def format_size(size):
    if size < 1024:
        return '%dB' % size
    if size < 1024 * 1024:
        return '%dKB' % (size // 1024)
    return '%dMB' % (size // 1024 // 1024)


def _parse_manifest(raw):
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _package_error('invalid YAML (%s)' % exc, file=MANIFEST_NAME)
    if not isinstance(data, dict):
        raise _package_error('must be a YAML dictionary', file=MANIFEST_NAME)

    if 'schema' not in data:
        raise _package_error('schema is required', file=MANIFEST_NAME)
    schema = data['schema']
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _package_error(
            'schema must be an integer (supported: %d)' % SUPPORTED_SCHEMA, file=MANIFEST_NAME,
        )
    if schema != SUPPORTED_SCHEMA:
        # Un esquema que el servidor no conoce es un pedido que no se puede
        # interpretar, no un paquete mal formado: la API lo distingue con 422.
        raise _package_error(
            'unknown schema %r (supported: %d)' % (schema, SUPPORTED_SCHEMA), file=MANIFEST_NAME,
            code='unsupported_schema',
        )

    unknown = sorted(set(data) - MANIFEST_KEYS)
    if unknown:
        raise _package_error('unknown key(s): %s' % ', '.join(unknown), file=MANIFEST_NAME)

    title = data.get('title')
    if not isinstance(title, str) or not title.strip():
        raise _package_error('title is required and must be a non-empty string', file=MANIFEST_NAME)
    if len(title) > Problem._meta.get_field('name').max_length:
        raise _package_error('title is too long', file=MANIFEST_NAME)

    limits = _parse_limits(data.get('limits'))
    types = _parse_names(data.get('types'), 'types')
    group = data.get('group')
    if not isinstance(group, str) or not group.strip():
        raise _package_error('group is required and must be a non-empty string', file=MANIFEST_NAME)
    group = group.strip()
    if len(group) > ProblemGroup._meta.get_field('name').max_length:
        raise _package_error('group name is too long', file=MANIFEST_NAME)

    visibility = data.get('visibility')
    if not isinstance(visibility, str) or visibility not in VISIBILITIES:
        raise _package_error(
            'visibility must be one of: %s' % ', '.join(sorted(VISIBILITIES)), file=MANIFEST_NAME,
        )

    time_limit = data.get('time_limit')
    if time_limit is not None:
        time_limit = _parse_time_limit(time_limit, 'time_limit')

    memory_limit = data.get('memory_limit')
    if memory_limit is not None:
        memory_limit = _parse_memory_limit(memory_limit, 'memory_limit')

    points = data.get('points')
    if points is not None:
        if not _is_number(points) or points < settings.DMOJ_PROBLEM_MIN_PROBLEM_POINTS:
            raise _package_error('points must be a non-negative number', file=MANIFEST_NAME)
        points = float(points)

    return _Manifest(
        title=title, limits=limits, types=types, group=group,
        is_public=VISIBILITIES[visibility], time_limit=time_limit,
        memory_limit=memory_limit, points=points,
    )


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_time_limit(value, label):
    """Valida un límite de tiempo contra los mismos topes que el modelo."""
    if not _is_number(value) or value <= 0:
        raise _package_error('%s must be a positive number' % label, file=MANIFEST_NAME)
    value = float(value)
    maximum = settings.DMOJ_PROBLEM_MAX_TIME_LIMIT
    if value > maximum:
        raise _package_error('%s is over the %ss limit' % (label, maximum), file=MANIFEST_NAME)
    return value


def _parse_memory_limit(value, label):
    """Valida un límite de memoria contra los mismos topes que el modelo."""
    if not _is_number(value) or value <= 0 or int(value) != value:
        raise _package_error('%s must be a positive integer' % label, file=MANIFEST_NAME)
    value = int(value)
    maximum = settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT
    if value > maximum:
        raise _package_error('%s is over the %sKB limit' % (label, maximum), file=MANIFEST_NAME)
    return value


def _parse_names(value, key):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise _package_error('%s must be a list of strings' % key, file=MANIFEST_NAME)
    names = []
    seen = set()
    for name in value:
        name = name.strip()
        if not name:
            raise _package_error('%s must not contain empty names' % key, file=MANIFEST_NAME)
        if len(name) > ProblemType._meta.get_field('name').max_length:
            raise _package_error('%s name is too long: %s' % (key, name), file=MANIFEST_NAME)
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def _parse_limits(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _package_error('limits must be a mapping of extension to limits', file=MANIFEST_NAME)
    limits = {}
    for extension, limit in value.items():
        if not isinstance(extension, str):
            raise _package_error('limit extensions must be strings', file=MANIFEST_NAME)
        normalized = extension.strip().lower().lstrip('.')
        if not _EXTENSION_RE.match(normalized):
            raise _package_error('invalid language extension %r' % extension, file=MANIFEST_NAME)
        if not isinstance(limit, dict):
            raise _package_error('limits.%s must be a mapping' % extension, file=MANIFEST_NAME)
        unknown = sorted(set(limit) - LIMIT_KEYS)
        if unknown:
            raise _package_error(
                'unknown key(s) in limits.%s: %s' % (extension, ', '.join(unknown)), file=MANIFEST_NAME,
            )
        time_limit = _parse_time_limit(limit.get('tl'), 'limits.%s.tl' % normalized)
        memory_limit = _parse_memory_limit(limit.get('ml'), 'limits.%s.ml' % normalized)
        limits[normalized] = (time_limit, memory_limit)
    return limits


def _parse_init(raw):
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _package_error('invalid YAML (%s)' % exc, file=INIT_NAME)
    if not isinstance(data, dict):
        raise _package_error('must be a YAML dictionary', file=INIT_NAME)
    return data


def _init_file_reference(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get('name')
        if isinstance(name, str):
            return name
    return None


#: Campo de ``ProblemData`` que corresponde a cada clave de ``init.yml``.
_INIT_FILE_FIELDS = {
    'checker': 'checker',
    'grader': 'grader',
    'custom_judge': 'grader',
    'signature': 'signature',
    'evaluator': 'evaluator',
}

#: Extensiones aceptadas por campo de ``ProblemData``.
_FIELD_EXTENSIONS = {
    'checker': ('.py',),
    'grader': ('.py',),
    'signature': ('.hpp',),
    'evaluator': ('.cpp',),
}

_CANONICAL_FILES = {
    'checker': 'checker.py',
    'grader': 'grader.py',
    'signature': 'signature.hpp',
    'evaluator': 'evaluator.cpp',
}


def _validate_optional_files(entries, init_yml):
    """Valida los archivos del juez y devuelve ``(archivos, campos)``.

    ``archivos`` mapea nombre a bytes y ``campos`` mapea el campo de
    ``ProblemData`` al nombre que lo contiene.
    """
    files = {}
    fields = {}

    for field, canonical in _CANONICAL_FILES.items():
        if canonical in entries:
            files[canonical] = entries[canonical]
            fields[field] = canonical

    for key in INIT_FILE_KEYS:
        reference = _init_file_reference(init_yml.get(key))
        if not reference:
            continue
        if not _SAFE_ENTRY_RE.match(reference):
            raise _package_error('invalid file reference in init.yml', file=reference)
        field = _INIT_FILE_FIELDS[key]
        extension = os.path.splitext(reference)[1].lower()
        if extension not in _FIELD_EXTENSIONS[field]:
            raise _package_error(
                'file extension is not allowed for %s' % key, file=reference,
            )
        if reference not in entries:
            raise _package_error(
                'referenced by init.yml but not present in the package', file=reference,
            )
        files[reference] = entries[reference]
        fields[field] = reference

    # La ruta vieja exigía el par: el juez no puede correr una firma sin su
    # evaluador, así que aceptarla sería publicar un problema que no califica.
    if 'signature' in fields and 'evaluator' not in fields:
        raise _package_error('signature requires an evaluator', file=fields['signature'])

    return files, fields


def _validate_root_entries(entries, allowed):
    for name in entries:
        if name.startswith(MEDIA_PREFIX):
            continue
        if name not in allowed:
            raise _package_error('file is not allowed at the package root', file=name)


def _media_formats():
    return tuple(str(extension).lower() for extension in getattr(
        settings, 'DMOJ_PROBLEM_MEDIA_ALLOWED_FORMATS', DEFAULT_MEDIA_FORMATS,
    ))


def _collect_media(entries):
    allowed_formats = _media_formats()
    max_size = getattr(settings, 'DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE', None)
    max_files = getattr(settings, 'DMOJ_PROBLEM_MEDIA_MAX_FILES', None)
    media = {}
    for name, content in entries.items():
        if not name.startswith(MEDIA_PREFIX):
            continue
        relative = name[len(MEDIA_PREFIX):]
        if not relative:
            continue
        extension = os.path.splitext(relative)[1].lower()
        if extension not in allowed_formats:
            raise _package_error('media file extension is not allowed', file=name)
        if max_size is not None and len(content) > max_size:
            raise _package_error('media file is too big (max %s)' % format_size(max_size), file=name)
        media[relative] = content
    if max_files is not None and len(media) > max_files:
        raise _package_error('too many media files (max %d)' % max_files, file=MEDIA_PREFIX)
    return media

# ---------------------------------------------------------------------------
# Enunciado -> description
# ---------------------------------------------------------------------------

# Esquemas que una referencia de enunciado nunca debe reescribirse.
_EXTERNAL_URL_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')


def _media_url_base():
    """URL pública de la raíz de medios de problemas, o ``None``.

    La deduce de ``MEDIA_URL`` y la posición de ``DMOJ_PROBLEM_MEDIA_ROOT``
    dentro de ``MEDIA_ROOT``; es la misma regla que usa la vista de medios.
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


def _resolve_statement_reference(url, media_names, base, warnings):
    """Reescribe una referencia relativa del enunciado a la URL de medios.

    ``http(s)://``, anclas y esquemas como ``mailto:`` quedan intactos, y las
    rutas absolutas (que empiezan con ``/``) también: no son del enunciado.
    Una referencia relativa se resuelve contra la raíz de medios del
    problema; un ``media/`` inicial se quita, y un archivo que no está en el
    paquete es un aviso: el enlace queda visible roto en el sitio y no
    silencioso.
    """
    url = url.strip()
    if not url or url.startswith('#') or url.startswith('/') or _EXTERNAL_URL_RE.match(url):
        return url
    relative = url[len(MEDIA_PREFIX):] if url.startswith(MEDIA_PREFIX) else url
    relative = relative.lstrip('/')
    target = base + quote(relative) if base is not None else '/' + relative
    if media_names is not None and relative not in media_names:
        warnings.append('statement references %r which is not in media/' % url)
    return target


def _derive_description_from_markdown(text, media_names, base, warnings):
    """Convierte el markdown del enunciado en la ``description`` del problema.

    Reescribe los destinos relativos de enlaces e imágenes a la URL pública de
    medios del problema. No toca código, anclas ni URLs absolutas.
    """
    link_re = re.compile(r'(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')

    def replace(match):
        marker, label, url = match.group(1), match.group(2), match.group(3)
        resolved = _resolve_statement_reference(url, media_names, base, warnings)
        return '%s[%s](%s)' % (marker, label, resolved)

    return link_re.sub(replace, text)



def derive_description(code, package, warnings=None):
    """``description`` del problema ``code`` que surge de ``package``.

    El enunciado markdown se publica con sus referencias relativas
    reescritas a la URL pública de medios; el pdf queda como un "embed"
    al archivo servido. ``warnings`` opcional acumula las referencias que no
    se pudieron resolver contra los medios del paquete.
    """
    if package.statement_name == STATEMENT_PDF_NAME:
        return _derive_description_from_pdf(_media_url_base_for(code))
    if warnings is None:
        warnings = []
    media_names = set(package.media) | {package.statement_name}
    return _derive_description_from_markdown(
        package.statement_text, media_names, _media_url_base_for(code), warnings,
    )


def _media_url_base_for(code):
    base = _media_url_base()
    quoted = quote(code) + '/'
    if base is None:
        # Sin ``MEDIA_ROOT``/``MEDIA_URL`` no se puede deducir la URL pública;
        # asumimos el layout estándar del despliegue.
        return '/media/problems/' + quoted
    return urljoin(base, quoted)


def _derive_description_from_pdf(base):
    """Enunciado pdf: un ``<embed>`` al pdf servido como medio del problema.

    La description resultante es HTML crudo, así que el problema se marca con
    ``is_full_markup`` para que DMOJ la publique sin filtrar.
    """
    if base is None:
        base = '/'
    return (
        '<embed style="overflow: scroll; height: 75vh; width: 100%; pointer-events: auto;" '
        'src="{}{}">'.format(base, STATEMENT_PDF_NAME)
    )


def _validate_case_archive(archive_name, archive_bytes, init_yml):
    names = _read_nested_archive(archive_name, archive_bytes)
    try:
        # Sin validar nombres todavía: la estructura primero, así una referencia
        # ausente se informa con el nombre del caso y no con el del zip.
        load_case_specs(init_yml, True)
    except ValidationError as exc:
        raise _package_error('; '.join(exc.messages), file=INIT_NAME)

    for case in init_yml.get('test_cases', []):
        if not isinstance(case, dict):
            continue
        subcases = case.get('batched')
        if isinstance(subcases, list):
            for subcase in subcases:
                if not isinstance(subcase, dict):
                    continue
                for key in ('in', 'out'):
                    if subcase.get(key) not in names:
                        raise _package_error(
                            'referenced by init.yml but not found in %s' % archive_name,
                            file=subcase.get(key),
                        )
        else:
            for key in ('in', 'out'):
                if case.get(key) not in names:
                    raise _package_error(
                        'referenced by init.yml but not found in %s' % archive_name,
                        file=case.get(key),
                    )
    return names


def _load_package(source):
    entries = _read_entries(_read_source(source))

    if MANIFEST_NAME not in entries:
        raise _package_error('missing required file', file=MANIFEST_NAME)
    if INIT_NAME not in entries:
        raise _package_error('missing required file', file=INIT_NAME)
    statement_names = [name for name in STATEMENT_NAMES if name in entries]
    if not statement_names:
        raise _package_error(
            'missing required file (exactly one of %s)' % ', '.join(STATEMENT_NAMES),
            file=STATEMENT_MD_NAME,
        )
    if len(statement_names) > 1:
        raise _package_error(
            'package has %s; include exactly one of them' % ' and '.join(statement_names),
            file=statement_names[1],
        )

    manifest = _parse_manifest(entries[MANIFEST_NAME])
    init_yml = _parse_init(entries[INIT_NAME])

    archive_name = init_yml.get('archive')
    if not isinstance(archive_name, str) or not archive_name.strip():
        raise _package_error('archive is required and must name the case zip', file=INIT_NAME)
    archive_name = archive_name.strip()
    if not _SAFE_ENTRY_RE.match(archive_name) or not archive_name.lower().endswith('.zip'):
        raise _package_error('archive must be a .zip file at the package root', file=INIT_NAME)
    if archive_name not in entries:
        raise _package_error('archive named by init.yml is missing', file=archive_name)

    optional, file_fields = _validate_optional_files(entries, init_yml)
    allowed = {MANIFEST_NAME, INIT_NAME, archive_name}
    allowed.update(STATEMENT_NAMES)
    allowed.update(optional)
    _validate_root_entries(entries, allowed)

    statement_name = statement_names[0]
    statement_bytes = entries[statement_name]
    if statement_name == STATEMENT_MD_NAME:
        try:
            statement_text = statement_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise _package_error('statement is not valid UTF-8 (%s)' % exc, file=statement_name)
    else:
        statement_text = None

    archive_bytes = entries[archive_name]
    _validate_case_archive(archive_name, archive_bytes, init_yml)

    media = _collect_media(entries)

    return _Package(
        manifest=manifest,
        init_bytes=entries[INIT_NAME],
        init_yml=init_yml,
        archive_name=archive_name,
        archive_bytes=archive_bytes,
        statement_name=statement_name,
        statement_bytes=statement_bytes,
        statement_text=statement_text,
        optional=optional,
        file_fields=file_fields,
        media=media,
    )


# ---------------------------------------------------------------------------
# Carga de init.yml -> casos
# ---------------------------------------------------------------------------

def load_case_specs(inityml, valid_files):
    """Traduce ``init.yml`` a las filas de ``ProblemTestCase``.

    ``valid_files`` es el conjunto de nombres dentro del zip de casos, o
    ``True`` para no validar nombres. Devuelve diccionarios con ``order``,
    ``type``, ``input_file``, ``output_file`` y ``points``.
    """
    if not isinstance(inityml, dict):
        raise ValidationError(_('%s must be a YAML dictionary') % INIT_NAME)
    if 'test_cases' not in inityml:
        raise ValidationError(_('test_cases must be defined in init.yml'))
    if not isinstance(inityml['test_cases'], list):
        raise ValidationError(_('test_cases must be a list in init.yml'))

    valid = valid_files if isinstance(valid_files, bool) else set(valid_files)

    def is_valid(name):
        return valid if isinstance(valid, bool) else name in valid

    specs = []
    pos = 1
    for c, case in enumerate(inityml['test_cases']):
        if not isinstance(case, dict):
            raise ValidationError(_('Test #%d must be a mapping') % c)
        if 'points' not in case:
            raise ValidationError(_('Test/batch #%d does not have points') % c)

        if 'batched' in case:
            if not isinstance(case['batched'], list):
                raise ValidationError(_('Test/batch #%d batched must be a list') % c)
            specs.append({'order': pos, 'type': 'S', 'input_file': '', 'output_file': '',
                          'points': case['points']})
            pos += 1
            for s, subcase in enumerate(case['batched']):
                if not isinstance(subcase, dict) or 'in' not in subcase or 'out' not in subcase:
                    raise ValidationError(_('Test #%(case)d.%(subcase)d does not have input and output') % {
                        'case': c,
                        'subcase': s,
                    })
                # Un ``in: [1.in]`` no es un nombre de archivo; sin este chequeo
                # el valor crudo termina hasheado y levanta TypeError.
                if not isinstance(subcase['in'], str) or not isinstance(subcase['out'], str):
                    raise ValidationError(_('Test #%(case)d.%(subcase)d: input and output must be file names') % {
                        'case': c,
                        'subcase': s,
                    })
                if not is_valid(subcase['in']):
                    raise ValidationError(_('Test #%(case)d.%(subcase)d: %(inp)s is not a valid file') % {
                        'case': c,
                        'subcase': s,
                        'inp': subcase['in'],
                    })
                if not is_valid(subcase['out']):
                    raise ValidationError(_('Test #%(case)d.%(subcase)d: %(outp)s is not a valid file') % {
                        'case': c,
                        'subcase': s,
                        'outp': subcase['out'],
                    })
                specs.append({'order': pos, 'type': 'C', 'input_file': subcase['in'],
                              'output_file': subcase['out'], 'points': None})
                pos += 1
            specs.append({'order': pos, 'type': 'E', 'input_file': '', 'output_file': '', 'points': None})
            pos += 1
        else:
            if 'in' not in case or 'out' not in case:
                raise ValidationError(_('Test #%d does not have input and output') % c)
            if not isinstance(case['in'], str) or not isinstance(case['out'], str):
                raise ValidationError(_('Test #%d: input and output must be file names') % c)
            if not is_valid(case['in']):
                raise ValidationError(_('Test #%(case)d: %(inp)s is not a valid file') % {
                    'case': c,
                    'inp': case['in'],
                })
            if not is_valid(case['out']):
                raise ValidationError(_('Test #%(case)d: %(outp)s is not a valid file') % {
                    'case': c,
                    'outp': case['out'],
                })
            specs.append({'order': pos, 'type': 'C', 'input_file': case['in'],
                          'output_file': case['out'], 'points': case['points']})
            pos += 1
    return specs


# ---------------------------------------------------------------------------
# Escritura atómica
# ---------------------------------------------------------------------------

def _data_root():
    root = getattr(settings, 'DMOJ_PROBLEM_DATA_ROOT', None)
    if not root:
        raise _package_error('DMOJ_PROBLEM_DATA_ROOT is not configured')
    return root


def _media_root():
    root = getattr(settings, 'DMOJ_PROBLEM_MEDIA_ROOT', None)
    if not root:
        raise _package_error('DMOJ_PROBLEM_MEDIA_ROOT is not configured')
    return root


def _write_tree(root, files):
    os.makedirs(root)
    for relative, content in files.items():
        target = os.path.join(root, *relative.split('/'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(content)


def _tree_files(root):
    if not os.path.isdir(root):
        return {}
    found = {}
    for current, _, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(current, filename)
            found[os.path.relpath(path, root)] = path
    return found


def _same_file(left, right):
    if os.path.getsize(left) != os.path.getsize(right):
        return False
    with open(left, 'rb') as a, open(right, 'rb') as b:
        while True:
            chunk_a = a.read(65536)
            chunk_b = b.read(65536)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def _tree_matches(live, staged):
    live_files = _tree_files(live)
    staged_files = _tree_files(staged)
    if set(live_files) != set(staged_files):
        return False
    return all(_same_file(live_files[name], staged_files[name]) for name in staged_files)


def _swap_tree(staged, live, backup):
    """Instala ``staged`` en ``live``; el llamador restaura ante un error."""
    if os.path.exists(live):
        os.replace(live, backup)
    try:
        os.replace(staged, live)
    except OSError:
        if os.path.exists(backup):
            os.replace(backup, live)
        raise


def _restore_tree(staged, live, backup):
    if os.path.exists(live):
        os.replace(live, staged)
    if os.path.exists(backup):
        os.replace(backup, live)


def _manifest_matches(problem, package):
    manifest = package.manifest
    if problem.name != manifest.title:
        return False
    if problem.is_full_markup != (package.statement_name == STATEMENT_PDF_NAME):
        return False
    if problem.description != derive_description(problem.code, package):
        return False
    if problem.is_public != manifest.is_public:
        return False
    if problem.group is None or problem.group.name != manifest.group:
        return False
    if set(problem.types.values_list('name', flat=True)) != set(manifest.types):
        return False
    if manifest.time_limit is not None and problem.time_limit != manifest.time_limit:
        return False
    if manifest.memory_limit is not None and problem.memory_limit != manifest.memory_limit:
        return False
    if manifest.points is not None and problem.points != manifest.points:
        return False
    return True


def _limits_match(problem, manifest):
    declared = {}
    for extension, (time_limit, memory_limit) in manifest.limits.items():
        languages = Language.objects.filter(extension__iexact=extension)
        for language in languages:
            declared[language.id] = (time_limit, memory_limit)
    existing = {
        limit.language_id: (limit.time_limit, limit.memory_limit)
        for limit in LanguageLimit.objects.filter(problem=problem)
    }
    return existing == declared


def _cases_match(problem, specs):
    existing = list(problem.cases.order_by('order').values_list(
        'order', 'type', 'input_file', 'output_file', 'points',
    ))
    return existing == [
        (spec['order'], spec['type'], spec['input_file'], spec['output_file'], spec['points'])
        for spec in specs
    ]


def _validate_case_points(specs):
    for spec in specs:
        points = spec['points']
        if points is not None and (not _is_number(points) or int(points) != points):
            raise _package_error(
                'test #%d has a non-integer point value' % spec['order'], file=INIT_NAME,
            )
        if points is not None and points < 0:
            raise _package_error(
                'test #%d has a negative point value' % spec['order'], file=INIT_NAME,
            )


def _resolve_group(name, warnings):
    group, created = ProblemGroup.objects.get_or_create(name=name, defaults={'full_name': name})
    if created:
        warnings.append('group %r was created' % name)
    return group


def _resolve_types(names, warnings):
    types = []
    for name in names:
        problem_type, created = ProblemType.objects.get_or_create(name=name, defaults={'full_name': name})
        if created:
            warnings.append('type %r was created' % name)
        types.append(problem_type)
    return types


def _apply_metadata(problem, package, specs, created, warnings, author=None):
    manifest = package.manifest
    group = _resolve_group(manifest.group, warnings)
    types = _resolve_types(manifest.types, warnings)
    previous = None if created else (problem.points, problem.is_public)

    if created:
        problem.code = problem.code or str(uuid.uuid4())
        problem.name = manifest.title
        problem.group = group
        problem.time_limit = manifest.time_limit if manifest.time_limit is not None else DEFAULT_TIME_LIMIT
        problem.memory_limit = manifest.memory_limit if manifest.memory_limit is not None else DEFAULT_MEMORY_LIMIT
        problem.points = manifest.points if manifest.points is not None else DEFAULT_POINTS
        problem.is_public = manifest.is_public
        problem.description = derive_description(problem.code, package, warnings)
        problem.is_full_markup = package.statement_name == STATEMENT_PDF_NAME
        problem.save()
        problem.allowed_languages.set(Language.objects.all())
        if author is not None:
            # El que publica queda como autor, dentro de la misma transacción:
            # es la regla de edición que lo habilita a reemplazar el problema.
            problem.authors.add(author)
    else:
        problem.name = manifest.title
        problem.description = derive_description(problem.code, package, warnings)
        problem.is_full_markup = package.statement_name == STATEMENT_PDF_NAME
        problem.group = group
        problem.is_public = manifest.is_public
        if manifest.time_limit is not None:
            problem.time_limit = manifest.time_limit
        if manifest.memory_limit is not None:
            problem.memory_limit = manifest.memory_limit
        if manifest.points is not None:
            problem.points = manifest.points
        problem.save()
        if previous != (problem.points, problem.is_public):
            # El admin reencola por los mismos campos: los envíos guardan los
            # puntos del problema y los perfiles su total, y ninguno se entera
            # de un reemplazo si no se recalculan.
            from judge.tasks import rescore_problem
            transaction.on_commit(rescore_problem.s(problem.pk).delay)

    problem.types.set(types)

    if not _limits_match(problem, manifest):
        LanguageLimit.objects.filter(problem=problem).delete()
        limits = []
        for extension, (time_limit, memory_limit) in manifest.limits.items():
            languages = list(Language.objects.filter(extension__iexact=extension))
            if not languages:
                warnings.append('no language has extension .%s' % extension)
            limits.extend(
                LanguageLimit(problem=problem, language=language, time_limit=time_limit, memory_limit=memory_limit)
                for language in languages
            )
        LanguageLimit.objects.bulk_create(limits)

    if not _cases_match(problem, specs):
        problem.cases.all().delete()
        ProblemTestCase.objects.bulk_create(
            [ProblemTestCase(dataset=problem, **{key: spec[key] for key in
                                                 ('order', 'type', 'input_file', 'output_file', 'points')})
             for spec in specs]
        )

    data, _ = ProblemData.objects.get_or_create(problem=problem)
    code = problem.code
    fields = {
        'initfile': '%s/%s' % (code, INIT_NAME),
        'zipfile': '%s/%s' % (code, package.archive_name),
        'checker': '%s/%s' % (code, package.file_fields['checker'])
        if 'checker' in package.file_fields else None,
        'grader': '%s/%s' % (code, package.file_fields['grader'])
        if 'grader' in package.file_fields else None,
        'signature': '%s/%s' % (code, package.file_fields['signature'])
        if 'signature' in package.file_fields else None,
        'evaluator': '%s/%s' % (code, package.file_fields['evaluator'])
        if 'evaluator' in package.file_fields else None,
        'signature_grading': 'signature' in package.file_fields,
    }
    # ``ProblemData.save`` borra archivos viejos; el árbol ya se reemplaza
    # entero aparte, así que la fila se actualiza sin pasar por él.
    ProblemData.objects.filter(pk=data.pk).update(**fields)


def _check_public_visibility(manifest, user):
    """Exige el permiso de visibilidad del admin para publicar como público.

    El servicio es el único camino de escritura, así que el chequeo vive acá:
    una publicación con ``visibility: public`` sin ``user`` también se rechaza,
    en vez de confiar en que cada llamador se acuerde de mirar el manifiesto.
    """
    if not manifest.is_public:
        return
    if user is None or not (user.is_superuser or user.has_perm('judge.change_public_visibility')):
        raise _package_error(
            'publishing a public problem requires the change_public_visibility permission',
            file=MANIFEST_NAME, code='forbidden',
        )


def publish_package(source, problem=None, author=None, user=None):
    """Publica ``source`` (bytes, archivo o camino a un zip) en ``problem``.

    Con ``problem=None`` crea un problema nuevo y deja a ``author`` (un
    ``Profile``) como autor; si no, reemplaza el paquete vigente. ``user`` es
    quien publica y debe tener ``judge.change_public_visibility`` si el
    manifiesto declara el problema público, la misma regla que el admin.

    Valida todo antes de tocar disco o base y escribe los árboles de datos y
    medios en directorios temporales de la misma raíz, intercambiándolos recién
    al final. Ante cualquier fallo, el paquete anterior queda intacto.

    Devuelve un :class:`PublishResult`. Los errores de validación son
    :class:`PackageError` y nombran el archivo y el motivo.
    """
    package = _load_package(source)
    manifest = package.manifest

    _check_public_visibility(manifest, user)

    specs = load_case_specs(package.init_yml, _read_nested_archive(
        package.archive_name, package.archive_bytes,
    ))
    _validate_case_points(specs)

    created = problem is None
    if created:
        problem = Problem(code=str(uuid.uuid4()))
    elif not re.match(PROBLEM_ID_REGEX, problem.code or ''):
        raise _package_error('problem id must be a UUID', file=None)

    warnings = []
    if not package.media:
        warnings.append('the package has no media files')

    token = uuid.uuid4().hex
    data_root = _data_root()
    media_root = _media_root()
    data_live = os.path.join(data_root, problem.code)
    media_live = os.path.join(media_root, problem.code)
    data_stage = os.path.join(data_root, '.publish-data-%s-%s' % (problem.code, token))
    media_stage = os.path.join(media_root, '.publish-media-%s-%s' % (problem.code, token))
    data_backup = os.path.join(data_root, '.previous-data-%s-%s' % (problem.code, token))
    media_backup = os.path.join(media_root, '.previous-media-%s-%s' % (problem.code, token))

    swaps = []
    try:
        os.makedirs(data_root, exist_ok=True)
        os.makedirs(media_root, exist_ok=True)

        _write_tree(data_stage, package.data_files())
        # El enunciado también es un medio: vive con el resto de los archivos
        # del problema, en ``<raíz de medios>/<uuid>/statement.{md,pdf}``.
        staged_media = dict(package.media)
        staged_media[package.statement_name] = package.statement_bytes
        _write_tree(media_stage, staged_media)

        needs_data_swap = not _tree_matches(data_live, data_stage)
        needs_media_swap = not _tree_matches(media_live, media_stage)

        changed = created or needs_data_swap or needs_media_swap or not _manifest_matches(problem, package) \
            or not _limits_match(problem, manifest) or not _cases_match(problem, specs)

        try:
            with transaction.atomic():
                _apply_metadata(problem, package, specs, created, warnings, author=author)

                if needs_data_swap:
                    _swap_tree(data_stage, data_live, data_backup)
                    swaps.append((data_stage, data_live, data_backup))
                if needs_media_swap:
                    _swap_tree(media_stage, media_live, media_backup)
                    swaps.append((media_stage, media_live, media_backup))
        except Exception:
            for staged, live, backup in reversed(swaps):
                _restore_tree(staged, live, backup)
            swaps = []
            raise
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError(str(exc), file=None, code='publish_failed') from exc
    finally:
        for path in (data_stage, media_stage):
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)

    for _, _, backup in swaps:
        shutil.rmtree(backup, ignore_errors=True)

    files_written = tuple(sorted(package.data_files()))
    media_files = tuple(sorted(MEDIA_PREFIX + name for name in
                               dict(package.media, **{package.statement_name: package.statement_bytes})))
    return PublishResult(
        problem_id=problem.code,
        files_written=files_written,
        media_files=media_files,
        cases=len(specs),
        warnings=tuple(warnings),
        created=created,
        changed=changed,
    )
