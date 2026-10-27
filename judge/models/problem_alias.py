"""Nombres legibles de un problema.

Un alias es un nombre humano (`caballos`, `sum-of-two`) que redirige a la URL canónica del problema,
`/problem/<uuid>`. No es la identidad del problema y nunca se borra ni se reemplaza: renombrar un
problema agrega alias pero deja los viejos resolviendo. Se guardan en minúsculas, así que la
unicidad no distingue mayúsculas.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ['ProblemAlias', 'ALIAS_REGEX', 'normalize_alias']

ALIAS_MAX_LENGTH = 40
# Forma canónica (la que se guarda); las mayúsculas se aceptan y se normalizan en `clean`/`save`.
ALIAS_REGEX = r'^[a-z0-9_-]{1,%d}$' % ALIAS_MAX_LENGTH
ALIAS_RE = re.compile(ALIAS_REGEX)
# El validador corre antes de `clean`, así que acepta mayúsculas; lo guardado es siempre minúsculas.
ALIAS_VALIDATOR = RegexValidator(
    r'^[A-Za-z0-9_-]{1,%d}$' % ALIAS_MAX_LENGTH,
    _('Aliases must match ^[a-z0-9_-]{1,40}$.'),
)


def normalize_alias(name):
    return name.lower()


class ProblemAlias(models.Model):
    problem = models.ForeignKey('Problem', verbose_name=_('problem'), related_name='aliases',
                                on_delete=models.CASCADE)
    name = models.CharField(max_length=ALIAS_MAX_LENGTH, unique=True, verbose_name=_('alias'),
                            validators=[ALIAS_VALIDATOR],
                            help_text=_('Human-readable name that redirects to the problem URL.'))

    class Meta:
        ordering = ('name',)
        verbose_name = _('problem alias')
        verbose_name_plural = _('problem aliases')

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.name = normalize_alias(self.name or '')
        if not self.name:
            return
        duplicate = type(self).objects.filter(name=self.name).exclude(pk=self.pk).exists()
        if duplicate:
            raise ValidationError({
                'name': _('The alias "%(alias)s" is already in use by another problem.') % {'alias': self.name},
            })

    def save(self, *args, **kwargs):
        self.name = normalize_alias(self.name)
        return super().save(*args, **kwargs)

    @classmethod
    def create_for(cls, problem, name):
        """Crea un alias validado; lanza `ValidationError` legible si ya existe o es inválido."""
        alias = cls(problem=problem, name=name)
        alias.full_clean()
        alias.save()
        return alias

    @classmethod
    def resolve(cls, name):
        """Devuelve el alias que corresponde a `name` (sin distinguir mayúsculas), o `None`."""
        if not name:
            return None
        normalized = normalize_alias(name)
        if not ALIAS_RE.match(normalized):
            return None
        return cls.objects.select_related('problem').filter(name=normalized).first()
