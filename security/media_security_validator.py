"""
Módulo de Seguridad para Carga y Descarga de Archivos Multimedia en DMOJ
========================================================================
Provee validación profunda, desinfección y prevención de vulnerabilidades
para los formatos soportados: PNG, JPG/JPEG, PDF y ZIP.
"""

import os
import io
import re
import struct
import zipfile
from typing import Tuple, Optional, List
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.conf import settings

# Importación segura de Pillow
try:
    from PIL import Image, ImageOps, UnidentifiedImageError
    # Límite estricto de píxeles contra Decompression Bombs / Pixel Floods
    Image.MAX_IMAGE_PIXELS = 25_000_000  # Máx 25 MP (ej. 5000x5000)
except ImportError:
    Image = None

# Firmas de bytes mágicos (Magic Bytes)
MAGIC_SIGNATURES = {
    'png': b'\x89PNG\r\n\x1a\n',
    'jpg': b'\xFF\xD8\xFF',
    'pdf': b'%PDF-',
    'zip': (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'),
}

# Límites de cuotas y recursos
MAX_FILE_SIZE = getattr(settings, 'DMOJ_PROBLEM_MEDIA_MAX_FILE_SIZE', 10 * 1024 * 1024)  # 10 MB por defecto
MAX_ZIP_ENTRIES = 500
MAX_ZIP_UNCOMPRESSED_TOTAL = 50 * 1024 * 1024  # 50 MB
MAX_ZIP_COMPRESSION_RATIO = 20.0  # Máximo ratio decompresión / compresión
ALLOWED_ZIP_EXTENSIONS = {'.txt', '.in', '.out', '.ans', '.cpp', '.c', '.py', '.java', '.h', '.hpp', '.pdf', '.png', '.jpg'}


class SecurityValidationError(ValidationError):
    """Excepción específica para violaciones de políticas de seguridad."""
    pass


def sanitize_filename(filename: str) -> str:
    """
    Sanitiza el nombre del archivo eliminando secuencias de traversal,
    caracteres no ASCII/no alfanuméricos y extensiones dobles engañosas.
    """
    clean_name = os.path.basename(filename)
    # Reemplazar caracteres peligrosos
    clean_name = re.sub(r'[^a-zA-Z0-9_\.-]', '_', clean_name)
    # Evitar nombres ocultos o que comiencen por punto
    clean_name = clean_name.lstrip('.')
    if not clean_name:
        clean_name = 'unnamed_file'
    return clean_name


def verify_magic_bytes(data: bytes, format_type: str) -> bool:
    """Valida los magic bytes reales del flujo de bytes contra el formato esperado."""
    expected = MAGIC_SIGNATURES.get(format_type.lower())
    if not expected:
        return False
    if isinstance(expected, tuple):
        return any(data.startswith(sig) for sig in expected)
    return data.startswith(expected)


# =====================================================================
# 1. VALIDACIÓN Y DESINFECCIÓN DE IMÁGENES (PNG / JPG)
# =====================================================================

def sanitize_and_validate_image(file_obj, expected_format: str) -> Tuple[io.BytesIO, str]:
    """
    Valida y desinfecta imágenes (PNG o JPG):
    1. Verifica magic bytes.
    2. Decodifica el raster completo con Pillow para verificar integridad.
    3. Impide bombas de píxeles (Decompression Bombs).
    4. Re-codifica la imagen limpia en un buffer nuevo, eliminando metadatos
       EXIF, comentarios y chunks auxiliares (tEXt, iTXt, zTXt) donde se
       pueden ocultar cargas útiles de XSS o polyglots.
    """
    if Image is None:
        raise SecurityValidationError(_("El módulo Pillow no está disponible para procesar imágenes."))

    data = file_obj.read()
    file_obj.seek(0)

    if not verify_magic_bytes(data, expected_format):
        raise SecurityValidationError(_("El archivo no coincide con la firma binaria de %(fmt)s.") % {'fmt': expected_format})

    try:
        img_buffer = io.BytesIO(data)
        with Image.open(img_buffer) as img:
            img.verify()  # Valida consistencia estructural básica

        # Re-abrir para renderizado y sanitización completa (verify invalida el descriptor)
        img_buffer.seek(0)
        with Image.open(img_buffer) as img:
            format_upper = expected_format.upper()
            if format_upper == 'JPG':
                format_upper = 'JPEG'

            # Validar dimensiones
            width, height = img.size
            if width > 6000 or height > 6000:
                raise SecurityValidationError(_("Dimensiones de imagen excesivas (máximo 6000x6000)."))

            # Crear nueva imagen en memoria libre de metadatos o chunks maliciosos
            clean_buffer = io.BytesIO()

            if format_upper == 'PNG':
                # Preservar canal alfa si existe (RGBA / LA) o convertir a RGB
                mode = 'RGBA' if img.mode in ('RGBA', 'LA', 'P') else 'RGB'
                cleaned_img = img.convert(mode)
                cleaned_img.save(clean_buffer, format='PNG', optimize=True)
                mime_type = 'image/png'
            else:  # JPEG
                cleaned_img = img.convert('RGB')
                cleaned_img.save(clean_buffer, format='JPEG', quality=85, optimize=True)
                mime_type = 'image/jpeg'

            clean_buffer.seek(0)
            return clean_buffer, mime_type

    except Image.DecompressionBombError:
        raise SecurityValidationError(_("Alerta de seguridad: Decompression bomb detectada en la imagen."))
    except (UnidentifiedImageError, SyntaxError, ValueError, IOError) as exc:
        raise SecurityValidationError(_("Estructura de imagen inválida o corrupta: %(err)s") % {'err': str(exc)})


# =====================================================================
# 2. VALIDACIÓN Y DESINFECCIÓN DE ARCHIVOS PDF
# =====================================================================

# Patrones de contenido activo peligroso en PDFs
PDF_DANGEROUS_PATTERNS = [
    rb'/JavaScript\b',
    rb'/JS\b',
    rb'/Launch\b',
    rb'/SubmitForm\b',
    rb'/ImportData\b',
    rb'/RichMedia\b',
    rb'/GoToR\b',
    rb'/GoToE\b',
    rb'/OpenAction\b',
]

def validate_pdf_content(file_obj) -> str:
    """
    Valida un archivo PDF:
    1. Verifica cabecera mágica `%PDF-`.
    2. Inspecciona el contenido en busca de objetos de script y acciones activas
       que puedan ejecutar código o provocar fugas de sesión / XSS.
    3. Verifica la presencia de marcador de fin de archivo `%%EOF`.
    """
    data = file_obj.read()
    file_obj.seek(0)

    if not verify_magic_bytes(data, 'pdf'):
        raise SecurityValidationError(_("El archivo no posee la firma válida de formato PDF (%PDF-)."))

    # Búsqueda estricta de marcadores de contenido activo / JavaScript
    for pattern in PDF_DANGEROUS_PATTERNS:
        match = re.search(pattern, data, re.IGNORECASE)
        if match:
            obj_tag = match.group(0).decode('latin1', errors='ignore')
            raise SecurityValidationError(
                _("El archivo PDF contiene directivas activas peligrosas no permitidas (%(tag)s).") % {'tag': obj_tag}
            )

    # Validar terminador estándar de PDF
    if b'%%EOF' not in data[-2048:]:
        raise SecurityValidationError(_("El archivo PDF carece del marcador de cierre %%EOF o está truncado."))

    return 'application/pdf'


# =====================================================================
# 3. VALIDACIÓN DE ARCHIVOS COMPRIMIDOS ZIP (ANTI ZIP-SLIP & BOMBS)
# =====================================================================

def validate_zip_archive(file_obj) -> None:
    """
    Valida exhaustivamente un archivo ZIP:
    1. Verifica magic bytes.
    2. Mitiga Zip Slip (Directory Traversal).
    3. Mitiga Zip Bombs (análisis de ratio, tamaño acumulado y conteo de archivos).
    4. Prohíbe enlaces simbólicos (symlinks) y tipos de archivo no permitidos dentro del archivo.
    """
    data = file_obj.read()
    file_obj.seek(0)

    if not verify_magic_bytes(data, 'zip'):
        raise SecurityValidationError(_("El archivo no posee una cabecera válida de archivo ZIP."))

    try:
        zip_buffer = io.BytesIO(data)
        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            infolist = zf.infolist()

            if len(infolist) > MAX_ZIP_ENTRIES:
                raise SecurityValidationError(_("El archivo ZIP contiene demasiadas entradas (máximo %(max)d).") % {'max': MAX_ZIP_ENTRIES})

            total_uncompressed_bytes = 0

            for entry in infolist:
                # 1. Detección de Zip Slip / Directory Traversal
                name = entry.filename
                if name.startswith('/') or name.startswith('\\'):
                    raise SecurityValidationError(_("Zip Slip detectado: Ruta absoluta en entrada %(name)s.") % {'name': name})

                # Normalización y verificación de salida del directorio base
                normalized = os.path.normpath(name)
                if normalized.startswith('..') or '..' in normalized.split(os.sep):
                    raise SecurityValidationError(_("Zip Slip detectado: Ruta de escape de directorio en %(name)s.") % {'name': name})

                # 2. Detección de Enlaces Simbólicos (Symlinks)
                # En sistemas POSIX, los symlinks tienen el bit 0o120000 en el atributo externo
                is_symlink = (entry.external_attr >> 16) & 0o120000 == 0o120000
                if is_symlink:
                    raise SecurityValidationError(_("El ZIP contiene enlaces simbólicos (symlink), lo cual no está permitido: %(name)s") % {'name': name})

                # 3. Detección de Extensiones Prohibidas y Carpetas Ocultas de IDEs (Anti-RCE en Cliente)
                # Bloquear carpetas ocultas/sensibles (.vscode, .idea, .git, etc.)
                parts = normalized.split(os.sep)
                if any(part.startswith('.') for part in parts):
                    raise SecurityValidationError(_("El ZIP contiene carpetas o archivos ocultos no permitidos: %(name)s") % {'name': name})

                if not entry.is_dir():
                    _, ext = os.path.splitext(name.lower())
                    if ext not in ALLOWED_ZIP_EXTENSIONS:
                        raise SecurityValidationError(_("Extensión peligrosa o no permitida dentro del archivo ZIP: %(name)s") % {'name': name})

                # 4. Prevención de Zip Bomb (Ratio y Tamaño Total)
                compressed_size = max(entry.compress_size, 1)
                ratio = entry.file_size / compressed_size
                if ratio > MAX_ZIP_COMPRESSION_RATIO and entry.file_size > 1024 * 1024:
                    raise SecurityValidationError(
                        _("Alerta de Zip Bomb: Ratio de compresión sospechoso en %(name)s (ratio %(ratio).1f).") % {
                            'name': name, 'ratio': ratio
                        }
                    )

                total_uncompressed_bytes += entry.file_size
                if total_uncompressed_bytes > MAX_ZIP_UNCOMPRESSED_TOTAL:
                    raise SecurityValidationError(
                        _("Alerta de Zip Bomb: Tamaño total descomprimido excede el límite permitido (%(max)d MB).") % {
                            'max': MAX_ZIP_UNCOMPRESSED_TOTAL // (1024 * 1024)
                        }
                    )

    except zipfile.BadZipFile as exc:
        raise SecurityValidationError(_("Archivo ZIP inválido o corrupto: %(err)s") % {'err': str(exc)})


# =====================================================================
# 4. DESPACHADOR CENTRAL DE VALIDACIÓN
# =====================================================================

def process_and_sanitize_upload(file_obj, filename: str) -> Tuple[io.BytesIO, str, str]:
    """
    Función de entrada principal para el manejo seguro de uploads en DMOJ.
    Retorna: (buffer_limpio, mime_type, sanitized_filename)
    """
    clean_name = sanitize_filename(filename)
    _, ext = os.path.splitext(clean_name.lower())

    if file_obj.size > MAX_FILE_SIZE:
        raise SecurityValidationError(_("El archivo excede el tamaño máximo permitido (%(max)d MB).") % {
            'max': MAX_FILE_SIZE // (1024 * 1024)
        })

    if ext == '.png':
        buf, mime = sanitize_and_validate_image(file_obj, 'png')
        return buf, mime, clean_name
    elif ext in ('.jpg', '.jpeg'):
        buf, mime = sanitize_and_validate_image(file_obj, 'jpg')
        return buf, mime, clean_name
    elif ext == '.pdf':
        mime = validate_pdf_content(file_obj)
        file_obj.seek(0)
        buf = io.BytesIO(file_obj.read())
        return buf, mime, clean_name
    elif ext == '.zip':
        validate_zip_archive(file_obj)
        file_obj.seek(0)
        buf = io.BytesIO(file_obj.read())
        return buf, 'application/zip', clean_name
    else:
        raise SecurityValidationError(_("Formato de archivo no soportado: %(ext)s") % {'ext': ext})
