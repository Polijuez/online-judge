"""Se borra la tabla de alias: el id del problema vuelve a ser la URL.

Un alias era un nombre humano (`caballos`) que redirigía a `/problem/<uuid>`;
fuera la tabla, la única forma de llegar a un problema es su `Problem.code`,
igual que en DMOJ. La tabla la creó 0157 y 0158 la sembró con los slugs viejos
de cada problema migrado, así que esos nombres dejan de resolver con esta
migración: los enlaces `/problem/<slug>` anteriores al cambio a UUID dan 404.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0159_delete_problemmedia'),
    ]

    operations = [
        migrations.DeleteModel(
            name='ProblemAlias',
        ),
    ]
