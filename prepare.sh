## Script auxiliar para preparar todo lo necesario antes de levantar el server
uv run python manage.py makemessages -l es
uv run python manage.py makemessages -l en
uv run python manage.py compilemessages --exclude=pl
uv run python manage.py makemigrations