"""Publicar un paquete de problema: validación, escritura atómica y metadata.

Un paquete es un zip con las entradas en la raíz:

* ``problem.yml`` (obligatorio): el manifiesto del sitio, ``schema: 1``.
* ``init.yml`` (obligatorio): lo que el juez consume; su clave ``archive:``
  nombra el zip de casos.
* el zip de casos nombrado por ``init.yml`` (obligatorio).
* ``statement.md`` (obligatorio): el enunciado en markdown.
* ``checker.py``, ``grader.py``, ``signature.hpp`` y ``evaluator.cpp``
  (opcionales). También se aceptan los nombres que ``init.yml`` declare en
  ``checker``, ``grader``, ``signature``, ``evaluator`` o ``custom_judge``.
* ``media/...`` (opcional): archivos del enunciado.

El manifiesto tiene esta forma (``schema``, ``title``, ``group`` y
``visibility`` son obligatorias; ``aliases``, ``limits`` y ``types`` pueden
omitirse y quedan vacías; las claves marcadas como opcionales solo pisan el
valor del problema cuando están)::

    schema: 1
    title: "Nombre del problema"
    aliases: [nombre-legible, otro-alias]
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

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from judge.models import Language, LanguageLimit, Problem, ProblemAlias, ProblemData, ProblemGroup, ProblemTestCase, \
    ProblemType
from judge.models.problem import PROBLEM_ID_REGEX
from judge.models.problem_alias import ALIAS_MAX_LENGTH, ALIAS_RE, normalize_alias

__all__ = ['PackageError', 'PublishResult', 'ProblemDataLoader', 'load_case_specs', 'publish_package']

MANIFEST_NAME = 'problem.yml'
INIT_NAME = 'init.yml'
STATEMENT_NAME = 'statement.md'
MEDIA_PREFIX = 'media/'
SUPPORTED_SCHEMA = 1

#: Claves de ``init.yml`` que pueden nombrar un archivo del juez.
INIT_FILE_KEYS = ('checker', 'grader', 'signature', 'evaluator', 'custom_judge')

MANIFEST_KEYS = frozenset({
    'schema', 'title', 'aliases', 'limits', 'types', 'group', 'visibility',
    'time_limit', 'memory_limit', 'points',
})
LIMIT_KEYS = frozenset({'tl', 'ml'})
VISIBILITIES = {'public': True, 'private': False}

DEFAULT_TIME_LIMIT = 2.0
DEFAULT_MEMORY_LIMIT = 262144
DEFAULT_POINTS = 100

#: Formatos de medios aceptados cuando el despliegue no configura una lista.
DEFAULT_MEDIA_FORMATS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.md')

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
    aliases_added: tuple
    warnings: tuple
    created: bool
    changed: bool


@dataclass
class _Manifest:
    title: str
    aliases: tuple
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
    statement: str
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
    entries = {}
    try:
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

    aliases = _parse_aliases(data.get('aliases'))
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
        if not _is_number(time_limit) or time_limit <= 0:
            raise _package_error('time_limit must be a positive number', file=MANIFEST_NAME)
        time_limit = float(time_limit)

    memory_limit = data.get('memory_limit')
    if memory_limit is not None:
        if not _is_number(memory_limit) or memory_limit <= 0 or int(memory_limit) != memory_limit:
            raise _package_error('memory_limit must be a positive integer', file=MANIFEST_NAME)
        memory_limit = int(memory_limit)

    points = data.get('points')
    if points is not None:
        if not _is_number(points) or points < 0:
            raise _package_error('points must be a non-negative number', file=MANIFEST_NAME)
        points = float(points)

    return _Manifest(
        title=title, aliases=aliases, limits=limits, types=types, group=group,
        is_public=VISIBILITIES[visibility], time_limit=time_limit,
        memory_limit=memory_limit, points=points,
    )


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_aliases(value):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(alias, str) for alias in value):
        raise _package_error('aliases must be a list of strings', file=MANIFEST_NAME)
    aliases = []
    seen = set()
    for alias in value:
        alias = normalize_alias(alias.strip())
        if not alias:
            raise _package_error('alias must not be empty', file=MANIFEST_NAME)
        if len(alias) > ALIAS_MAX_LENGTH or not ALIAS_RE.match(alias):
            raise _package_error('invalid alias %r' % alias, file=MANIFEST_NAME)
        if alias not in seen:
            aliases.append(alias)
            seen.add(alias)
    return tuple(aliases)


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
        time_limit = limit.get('tl')
        memory_limit = limit.get('ml')
        if not _is_number(time_limit) or time_limit <= 0:
            raise _package_error('limits.%s.tl must be a positive number' % extension, file=MANIFEST_NAME)
        if not _is_number(memory_limit) or memory_limit <= 0 or int(memory_limit) != memory_limit:
            raise _package_error('limits.%s.ml must be a positive integer' % extension, file=MANIFEST_NAME)
        limits[normalized] = (float(time_limit), int(memory_limit))
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
    if STATEMENT_NAME not in entries:
        raise _package_error('missing required file', file=STATEMENT_NAME)

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
    allowed = {MANIFEST_NAME, INIT_NAME, STATEMENT_NAME, archive_name}
    allowed.update(optional)
    _validate_root_entries(entries, allowed)

    try:
        statement = entries[STATEMENT_NAME].decode('utf-8')
    except UnicodeDecodeError as exc:
        raise _package_error('statement is not valid UTF-8 (%s)' % exc, file=STATEMENT_NAME)

    archive_bytes = entries[archive_name]
    _validate_case_archive(archive_name, archive_bytes, init_yml)

    media = _collect_media(entries)

    return _Package(
        manifest=manifest,
        init_bytes=entries[INIT_NAME],
        init_yml=init_yml,
        archive_name=archive_name,
        archive_bytes=archive_bytes,
        statement=statement,
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


class ProblemDataLoader(object):
    """Creador de casos a partir de ``init.yml`` (lo comparten el servicio y el staff)."""

    def __init__(self, problem, inityml, valid_files, dry_run=False):
        self.problem = problem
        self.inityml = inityml
        self.valid_files = valid_files
        self.dry_run = dry_run

    def load(self):
        specs = load_case_specs(self.inityml, self.valid_files)
        if self.dry_run:
            return []
        return [ProblemTestCase(dataset=self.problem, **spec) for spec in specs]


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
    if problem.description != package.statement:
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

    if created:
        problem.code = problem.code or str(uuid.uuid4())
        problem.name = manifest.title
        problem.group = group
        problem.time_limit = manifest.time_limit if manifest.time_limit is not None else DEFAULT_TIME_LIMIT
        problem.memory_limit = manifest.memory_limit if manifest.memory_limit is not None else DEFAULT_MEMORY_LIMIT
        problem.points = manifest.points if manifest.points is not None else DEFAULT_POINTS
        problem.is_public = manifest.is_public
        problem.description = package.statement
        problem.save()
        problem.allowed_languages.set(Language.objects.all())
        if author is not None:
            # El que publica queda como autor, dentro de la misma transacción:
            # es la regla de edición que lo habilita a reemplazar el problema.
            problem.authors.add(author)
    else:
        problem.name = manifest.title
        problem.description = package.statement
        problem.group = group
        problem.is_public = manifest.is_public
        if manifest.time_limit is not None:
            problem.time_limit = manifest.time_limit
        if manifest.memory_limit is not None:
            problem.memory_limit = manifest.memory_limit
        if manifest.points is not None:
            problem.points = manifest.points
        problem.save()

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


def _add_aliases(problem, manifest, added):
    existing = set(problem.aliases.values_list('name', flat=True))
    for alias in manifest.aliases:
        if alias in existing:
            continue
        problem.add_alias(alias)
        existing.add(alias)
        added.append(alias)


def _check_alias_conflicts(manifest, problem):
    if not manifest.aliases:
        return
    conflicts = ProblemAlias.objects.filter(name__in=manifest.aliases)
    if problem is not None and problem.pk is not None:
        conflicts = conflicts.exclude(problem_id=problem.pk)
    conflict = conflicts.first()
    if conflict is not None:
        raise _package_error(
            'alias %r is already in use by another problem' % conflict.name, file=MANIFEST_NAME,
        )


def publish_package(source, problem=None, author=None):
    """Publica ``source`` (bytes, archivo o camino a un zip) en ``problem``.

    Con ``problem=None`` crea un problema nuevo y deja a ``author`` (un
    ``Profile``) como autor; si no, reemplaza el paquete vigente. Valida todo
    antes de tocar disco o base y escribe los árboles de datos y medios en
    directorios temporales de la misma raíz, intercambiándolos recién al final.
    Ante cualquier fallo, el paquete anterior queda intacto.

    Devuelve un :class:`PublishResult`. Los errores de validación son
    :class:`PackageError` y nombran el archivo y el motivo.
    """
    package = _load_package(source)
    manifest = package.manifest

    _check_alias_conflicts(manifest, problem)

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
        _write_tree(media_stage, package.media)

        needs_data_swap = not _tree_matches(data_live, data_stage)
        needs_media_swap = not _tree_matches(media_live, media_stage)

        changed = created or needs_data_swap or needs_media_swap or not _manifest_matches(problem, package) \
            or not _limits_match(problem, manifest) or not _cases_match(problem, specs) \
            or bool(set(manifest.aliases) - set(problem.aliases.values_list('name', flat=True)))

        try:
            with transaction.atomic():
                _apply_metadata(problem, package, specs, created, warnings, author=author)

                aliases_added = []
                _add_aliases(problem, manifest, aliases_added)

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
    media_files = tuple(sorted(MEDIA_PREFIX + name for name in package.media))
    return PublishResult(
        problem_id=problem.code,
        files_written=files_written,
        media_files=media_files,
        cases=len(specs),
        aliases_added=tuple(aliases_added),
        warnings=tuple(warnings),
        created=created,
        changed=changed,
    )
