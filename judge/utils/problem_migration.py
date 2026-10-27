"""Migrar el árbol de problemas al layout por UUID (ADR 0009).

El estado viejo guarda cada problema en ``<raíz de datos>/<conjunto>/<slug>/``
y sus medios en ``<raíz de medios>/<slug>/``. El nuevo usa como nombre de
directorio el id del problema (``Problem.code``, un UUID) en ambas raíces (ADR
0002). El slug que nombra el directorio viejo se resuelve con la tabla de
alias: la migración 0158 sembró un alias por problema con el código viejo, así
que el mapeo en disco es ``directorio -> alias -> problema -> uuid``.

:func:`build_plan` arma el plan sin tocar el disco y junta aparte las
divergencias (directorios sin problema y problemas sin directorio), que se
cuarentenan. :func:`apply_plan` escribe primero el manifiesto del mapeo y
después renombra cada directorio dentro del mismo filesystem; los renombres
son atómicos, así que un corte a mitad de camino se reanuda volviendo a correr
el comando. Nunca borra nada: los ``.git`` de cada conjunto, los ``judge.yml``
y los directorios en cuarentena quedan donde están.
"""
import os
import re
from dataclasses import dataclass

from django.conf import settings

from judge.models import Problem, ProblemAlias
from judge.models.problem import PROBLEM_ID_REGEX
from judge.models.problem_alias import normalize_alias

__all__ = [
    'MigrationError', 'MigrationPlan', 'ProblemPlan', 'QuarantineItem', 'ManifestRow',
    'STATE_MOVE', 'STATE_DONE', 'STATE_MISSING',
    'QUARANTINE_DATA_DIR', 'QUARANTINE_MEDIA_DIR', 'QUARANTINE_PROBLEM',
    'build_plan', 'apply_plan', 'manifest_rows', 'read_manifest', 'write_manifest',
    'default_manifest_path', 'render_manifest',
]

UUID_RE = re.compile(PROBLEM_ID_REGEX)

#: Nombres que nunca se mueven aunque estén en el nivel de un problema. Los
#: ``.git`` (de cada conjunto) y el resto de los ocultos ya se saltean abajo;
#: ``judge.yml`` vive al lado de los problemas, no dentro. La limpieza de todo
#: esto es del ticket 14.
PROTECTED_NAMES = frozenset({'judge.yml'})

STATE_MOVE = 'move'
STATE_DONE = 'done'
STATE_MISSING = 'missing'

QUARANTINE_DATA_DIR = 'data-dir'
QUARANTINE_MEDIA_DIR = 'media-dir'
QUARANTINE_PROBLEM = 'problem'

MANIFEST_ENV = 'DMOJ_PROBLEM_TREE_MANIFEST'
MANIFEST_NAME = 'problem-tree-manifest.tsv'

MANIFEST_COLUMNS = ('problemset', 'slug', 'problem_id', 'alias',
                    'data_from', 'data_to', 'media_from', 'media_to')
MANIFEST_QUARANTINE_TAG = 'cuarentena'


class MigrationError(Exception):
    """El plan no se puede aplicar (conflicto, destino ocupado, fallo de ``rename``)."""


@dataclass(frozen=True)
class Move:
    """Un ``rename`` de directorio dentro de una raíz."""

    source: str
    destination: str
    problemset: str = ''
    slug: str = ''

    @property
    def source_name(self):
        return os.path.basename(self.source)


@dataclass(frozen=True)
class QuarantineItem:
    """Una divergencia que se reporta y no se toca."""

    kind: str
    path: str
    detail: str


@dataclass
class ProblemPlan:
    """Qué le corresponde a un problema en la migración."""

    problem_id: str
    title: str
    data: object = None
    media: object = None
    state: str = STATE_MISSING

    @property
    def alias(self):
        """Alias que nombra el directorio viejo (o vacío si ya estaba migrado)."""
        if self.data is not None:
            return self.data.slug
        return ''

    @property
    def pending(self):
        return self.data is not None or self.media is not None


@dataclass
class MigrationPlan:
    data_root: str
    media_root: str
    problems: list
    quarantine: list
    conflicts: list

    @property
    def moves(self):
        return [item for item in self.problems if item.data is not None]

    @property
    def pending(self):
        return [item for item in self.problems if item.pending]

    def pending_media_for_done(self):
        return [item for item in self.problems if item.data is None and item.media is not None]


@dataclass(frozen=True)
class ManifestRow:
    """Una fila del manifiesto: el mapeo que dejó el corte."""

    problemset: str
    slug: str
    problem_id: str
    alias: str
    data_from: str
    data_to: str
    media_from: str
    media_to: str

    def completed_with(self, other):
        """Completa los campos vacíos con los de ``other`` (reanudación)."""
        return ManifestRow(*(
            mine or theirs for mine, theirs in zip(
                (self.problemset, self.slug, self.problem_id, self.alias,
                 self.data_from, self.data_to, self.media_from, self.media_to),
                (other.problemset, other.slug, other.problem_id, other.alias,
                 other.data_from, other.data_to, other.media_from, other.media_to),
            )
        ))


def default_manifest_path():
    """Dónde se guarda el manifiesto: env, o al lado de ``manage.py``."""
    return os.environ.get(MANIFEST_ENV, os.path.join(settings.BASE_DIR, MANIFEST_NAME))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _ignored(name):
    return name.startswith('.') or name in PROTECTED_NAMES


def _children(path):
    try:
        return sorted(os.listdir(path))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _is_uuid(name):
    return bool(UUID_RE.match(name))


def _relative(path, root):
    return os.path.relpath(path, root)


def _check_directory(path, relative, kind, quarantine):
    """Filtra symlinks y archivos; marca lo raro en cuarentena."""
    if os.path.islink(path):
        quarantine.append(QuarantineItem(kind, relative, 'es un symlink; no se sigue'))
        return False
    return os.path.isdir(path)


def build_plan(data_root, media_root=None):
    """Arma el plan de migración sin tocar el disco.

    ``data_root`` y ``media_root`` son absolutos (o se resuelven acá). El plan
    queda vacío de movimientos si el árbol ya está migrado; los problemas sin
    datos y los directorios sin problema van a ``quarantine``; y cualquier
    situación en la que dos orígenes peleen por el mismo destino, o en la que
    origen y destino existan a la vez, va a ``conflicts`` (el comando se
    rehúsa a aplicar).
    """
    data_root = os.path.abspath(data_root)
    media_root = os.path.abspath(media_root) if media_root else None

    if not os.path.isdir(data_root):
        raise MigrationError('la raíz de datos %s no existe' % data_root)
    if media_root and not os.path.isdir(media_root):
        raise MigrationError('la raíz de medios %s no existe' % media_root)

    problems = list(Problem.objects.only('id', 'code', 'name').order_by('code'))
    problems_by_code = {problem.code: problem for problem in problems}
    alias_to_code = dict(ProblemAlias.objects.values_list('name', 'problem__code'))

    data_moves = {}
    media_moves = {}
    media_present = set()
    migrated = set()
    quarantine = []
    conflicts = []
    destination_claims = {}

    # Raíz de datos: los conjuntos viejos son directorios que no son UUID; los
    # ya migrados viven planos en la raíz con el id como nombre.
    for name in _children(data_root):
        if _ignored(name):
            continue
        path = os.path.join(data_root, name)
        if not _check_directory(path, name, QUARANTINE_DATA_DIR, quarantine):
            continue

        if _is_uuid(name):
            if name not in problems_by_code:
                quarantine.append(QuarantineItem(
                    QUARANTINE_DATA_DIR, name,
                    'directorio con id UUID sin fila de problema',
                ))
            else:
                migrated.add(name)
            continue

        for child in _children(path):
            if _ignored(child):
                continue
            child_path = os.path.join(path, child)
            relative = '%s/%s' % (name, child)
            if not _check_directory(child_path, relative, QUARANTINE_DATA_DIR, quarantine):
                continue

            code = alias_to_code.get(normalize_alias(child))
            if code is None:
                quarantine.append(QuarantineItem(
                    QUARANTINE_DATA_DIR, relative,
                    'no hay problema con el alias %r' % child,
                ))
                continue

            destination = os.path.join(data_root, code)
            previous = destination_claims.get(destination)
            if previous is not None or code in data_moves:
                first = previous or _relative(data_moves[code].source, data_root)
                conflicts.append(
                    'dos orígenes pelean por el destino %s: %s y %s' % (code, first, relative),
                )
                continue
            if code in migrated or os.path.lexists(destination):
                conflicts.append(
                    'el destino %s ya existe y el origen %s también' % (code, relative),
                )
                continue

            destination_claims[destination] = relative
            data_moves[code] = Move(child_path, destination, problemset=name, slug=child)

    # Raíz de medios: mismo esquema, un nivel menos.
    if media_root:
        for name in _children(media_root):
            if _ignored(name):
                continue
            path = os.path.join(media_root, name)
            if not _check_directory(path, name, QUARANTINE_MEDIA_DIR, quarantine):
                continue

            if _is_uuid(name):
                if name not in problems_by_code:
                    quarantine.append(QuarantineItem(
                        QUARANTINE_MEDIA_DIR, name,
                        'directorio con id UUID sin fila de problema',
                    ))
                else:
                    media_present.add(name)
                continue

            code = alias_to_code.get(normalize_alias(name))
            if code is None:
                quarantine.append(QuarantineItem(
                    QUARANTINE_MEDIA_DIR, name,
                    'no hay problema con el alias %r' % name,
                ))
                continue

            destination = os.path.join(media_root, code)
            if code in media_moves:
                conflicts.append(
                    'dos directorios de medios pelean por el destino %s: %s y %s' % (
                        code, media_moves[code].source_name, name,
                    ),
                )
                continue
            if code in media_present or os.path.lexists(destination):
                conflicts.append(
                    'el destino de medios %s ya existe y el origen %s también' % (code, name),
                )
                continue

            media_moves[code] = Move(path, destination, slug=name)

    # Los medios de un problema sin datos también se cuarentenan: ese problema
    # es una de las divergencias y no se toca ninguna de sus partes.
    for code, move in list(media_moves.items()):
        if code not in data_moves and code not in migrated:
            quarantine.append(QuarantineItem(
                QUARANTINE_MEDIA_DIR, move.slug,
                'el problema %s no tiene directorio de datos' % code,
            ))
            del media_moves[code]

    plans = []
    for problem in problems:
        code = problem.code
        if not _is_uuid(code or ''):
            conflicts.append(
                'el problema %r no tiene id UUID (¿faltan las migraciones?)' % problem.name,
            )
            continue

        item = ProblemPlan(
            problem_id=code,
            title=problem.name,
            data=data_moves.get(code),
            media=media_moves.get(code),
        )
        if item.data is not None:
            item.state = STATE_MOVE
        elif code in migrated:
            item.state = STATE_DONE
        else:
            item.state = STATE_MISSING
            quarantine.append(QuarantineItem(
                QUARANTINE_PROBLEM, code,
                'sin directorio de datos (ningún alias nombra un directorio)',
            ))
        plans.append(item)

    return MigrationPlan(
        data_root=data_root,
        media_root=media_root,
        problems=plans,
        quarantine=quarantine,
        conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# Manifiesto
# ---------------------------------------------------------------------------

def _clean(value):
    return (value or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def manifest_rows(plan):
    """Las filas nuevas que deja esta corrida (los movimientos pendientes)."""
    rows = []
    for item in plan.moves:
        media = item.media
        rows.append(ManifestRow(
            problemset=item.data.problemset,
            slug=item.data.slug,
            problem_id=item.problem_id,
            alias=item.alias,
            data_from='%s/%s' % (item.data.problemset, item.data.slug),
            data_to=item.problem_id,
            media_from=media.slug if media is not None else '',
            media_to=item.problem_id if media is not None else '',
        ))
    for item in plan.pending_media_for_done():
        rows.append(ManifestRow(
            problemset='',
            slug=item.media.slug,
            problem_id=item.problem_id,
            alias=item.media.slug,
            data_from='',
            data_to=item.problem_id,
            media_from=item.media.slug,
            media_to=item.problem_id,
        ))
    return rows


def read_manifest(path):
    """Lee manifiesto y cuarentena ya registrados; tolera un archivo ausente."""
    rows = []
    quarantine = []
    try:
        with open(path, encoding='utf-8') as manifest:
            lines = manifest.readlines()
    except FileNotFoundError:
        return rows, quarantine

    for line in lines:
        line = line.rstrip('\n')
        if not line:
            continue
        if line.startswith('#'):
            parts = line.lstrip('#').strip().split('\t')
            if len(parts) == 4 and parts[0] == MANIFEST_QUARANTINE_TAG:
                quarantine.append(QuarantineItem(parts[1], parts[2], parts[3]))
            continue
        parts = line.split('\t')
        if len(parts) == len(MANIFEST_COLUMNS):
            rows.append(ManifestRow(*parts))
    return rows, quarantine


def render_manifest(plan, rows, quarantine):
    lines = [
        '# Migración del árbol de problemas al layout por UUID (ADR 0009).',
        '# raíz de datos: %s' % _clean(plan.data_root),
        '# raíz de medios: %s' % _clean(plan.media_root or '(sin configurar)'),
        '# %s' % '\t'.join(MANIFEST_COLUMNS),
    ]
    for row in rows:
        lines.append('\t'.join(_clean(value) for value in (
            row.problemset, row.slug, row.problem_id, row.alias,
            row.data_from, row.data_to, row.media_from, row.media_to,
        )))
    if quarantine:
        lines.append('')
        lines.append('# Cuarentena: divergencias que no se tocaron.')
    for item in quarantine:
        lines.append('# %s\t%s\t%s\t%s' % (
            MANIFEST_QUARANTINE_TAG, _clean(item.kind), _clean(item.path), _clean(item.detail),
        ))
    return '\n'.join(lines) + '\n'


def write_manifest(path, plan):
    """Escribe (o completa) el manifiesto; no reescribe si el contenido es igual."""
    new_rows = manifest_rows(plan)
    existing_rows, existing_quarantine = read_manifest(path)

    merged = {row.problem_id: row for row in existing_rows}
    for row in new_rows:
        current = merged.get(row.problem_id)
        merged[row.problem_id] = row if current is None else current.completed_with(row)
    rows = sorted(merged.values(), key=lambda row: (row.problemset, row.slug, row.problem_id))

    quarantine = {}
    for item in existing_quarantine + list(plan.quarantine):
        quarantine[(item.kind, item.path, item.detail)] = item
    items = [quarantine[key] for key in sorted(quarantine)]

    content = render_manifest(plan, rows, items)
    try:
        with open(path, encoding='utf-8') as current:
            if current.read() == content:
                return False
    except FileNotFoundError:
        pass

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8', newline='\n') as manifest:
        manifest.write(content)
        manifest.flush()
        os.fsync(manifest.fileno())
    os.replace(temporary, path)
    return True


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------

def _rename(move):
    if not os.path.lexists(move.source):
        raise MigrationError('el origen %s ya no existe' % move.source)
    if os.path.lexists(move.destination):
        raise MigrationError('el destino %s ya existe' % move.destination)
    try:
        os.rename(move.source, move.destination)
    except OSError as error:
        raise MigrationError('no se pudo mover %s a %s: %s' % (
            move.source, move.destination, error,
        ))


def apply_plan(plan, manifest_path=None, on_moved=None):
    """Aplica el plan y deja el manifiesto; devuelve cuántos problemas movió.

    El manifiesto se escribe antes de mover, así que un corte posterior deja
    registrado el mapeo. Cada ``rename`` es atómico y no se pisa un destino
    existente; volver a correr el comando termina lo que falte.
    """
    if plan.conflicts:
        raise MigrationError('el plan tiene %d conflicto(s)' % len(plan.conflicts))

    pending = plan.pending
    if not pending:
        return 0

    if manifest_path:
        write_manifest(manifest_path, plan)

    moved = 0
    for item in pending:
        if item.data is not None:
            _rename(item.data)
        if item.media is not None:
            _rename(item.media)
        moved += 1
        if on_moved is not None:
            on_moved(item)
    return moved
