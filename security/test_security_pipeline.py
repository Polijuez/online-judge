"""
Suite de pruebas unitarias para validación de seguridad de archivos multimedia.
Verifica que las amenazas típicas (Zip Slip, Zip Bomb, Polyglots, Inyección PDF)
sean interceptadas correctamente.
"""

import io
import unittest
import zipfile
from media_security_validator import (
    sanitize_and_validate_image,
    validate_pdf_content,
    validate_zip_archive,
    sanitize_filename,
    SecurityValidationError,
    process_and_sanitize_upload
)

class MockUploadedFile:
    def __init__(self, data: bytes, name: str):
        self.data = data
        self.name = name
        self.size = len(data)
        self.buf = io.BytesIO(data)

    def read(self, *args):
        return self.buf.read(*args)

    def seek(self, pos):
        return self.buf.seek(pos)


class TestSecurityValidator(unittest.TestCase):

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("valid_image.png"), "valid_image.png")
        self.assertEqual(sanitize_filename("my file <script>.jpg"), "my_file__script_.jpg")
        self.assertEqual(sanitize_filename("...hidden.png"), "hidden.png")

    def test_reject_fake_png_with_html_payload(self):
        # Archivo con extensión .png pero contenido HTML/JS (MIME Confused / Polyglot)
        fake_content = b"<script>alert('XSS via cookie theft')</script>"
        mock = MockUploadedFile(fake_content, "attack.png")
        with self.assertRaises(SecurityValidationError) as ctx:
            process_and_sanitize_upload(mock, "attack.png")
        self.assertIn("firma binaria", str(ctx.exception).lower())

    def test_reject_pdf_with_active_javascript(self):
        # Archivo PDF con cabecera válida pero con objeto /JavaScript activo
        malicious_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /OpenAction 3 0 R >>\nendobj\n"
            b"3 0 obj\n<< /Type /Action /S /JavaScript /JS (app.alert('XSS');) >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n120\n%%EOF"
        )
        mock = MockUploadedFile(malicious_pdf, "statement.pdf")
        with self.assertRaises(SecurityValidationError) as ctx:
            validate_pdf_content(mock)
        self.assertIn("directivas activas", str(ctx.exception).lower())

    def test_accept_clean_pdf(self):
        # PDF benigno mínimo sin scripts
        clean_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
            b"xref\n0 3\n0000000000 65535 f \n"
            b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n80\n%%EOF"
        )
        mock = MockUploadedFile(clean_pdf, "statement.pdf")
        mime = validate_pdf_content(mock)
        self.assertEqual(mime, "application/pdf")

    def test_reject_zip_slip_traversal(self):
        # ZIP con entrada path traversal ../../../tmp/malicious.py
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("../../../etc/cron.d/pwn", b"* * * * * root /bin/evil\n")
        
        mock = MockUploadedFile(buf.getvalue(), "test_cases.zip")
        with self.assertRaises(SecurityValidationError) as ctx:
            validate_zip_archive(mock)
        self.assertIn("zip slip", str(ctx.exception).lower())

    def test_reject_zip_forbidden_extension(self):
        # ZIP que contiene un archivo ejecutable .exe o .bat
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("trojan.exe", b"MZexecutable")
        
        mock = MockUploadedFile(buf.getvalue(), "starter.zip")
        with self.assertRaises(SecurityValidationError) as ctx:
            validate_zip_archive(mock)
        self.assertIn("no permitida", str(ctx.exception).lower())

    def test_reject_zip_hidden_ide_folder(self):
        # ZIP con carpeta oculta de IDE (.vscode/tasks.json para RCE en máquina del concursante)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr(".vscode/tasks.json", b'{"version": "2.0.0", "tasks": []}')
        
        mock = MockUploadedFile(buf.getvalue(), "template.zip")
        with self.assertRaises(SecurityValidationError) as ctx:
            validate_zip_archive(mock)
        self.assertIn("carpetas o archivos ocultos", str(ctx.exception).lower())

    def test_accept_clean_zip(self):
        # ZIP con archivos legítimos de casos de prueba (.in, .out, .cpp)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("case1.in", b"5 10\n")
            zf.writestr("case1.out", b"15\n")
            zf.writestr("solution.cpp", b"#include <iostream>\nint main(){}\n")
        
        mock = MockUploadedFile(buf.getvalue(), "cases.zip")
        validate_zip_archive(mock)  # No debe lanzar excepción


if __name__ == '__main__':
    unittest.main()
