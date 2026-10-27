"""Avisar a los jueces que el árbol de problemas cambió.

Publicar deja el paquete nuevo en disco y en la base, pero un juez sigue
calificando con lo que escaneó hasta su próximo sondeo. Después de una
publicación exitosa el sitio llama ``POST /update/problems`` en la API de
control de cada juez para que reescanee el árbol en el acto (ADR 0008).

El aviso es best-effort: los endpoints salen de
``DMOJ_JUDGE_CONTROL_ENDPOINTS`` —una URL por juez, vacío desactiva el
aviso— y cada uno tiene ``DMOJ_JUDGE_CONTROL_TIMEOUT`` segundos. Los avisos
salen en paralelo y la espera total está acotada por ese timeout, así que un
juez caído o colgado no revierte la publicación, no cambia la respuesta y no
la demora más que el timeout; la falla queda en el log y el juez toma los
datos nuevos cuando vuelva.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger('judge.problem_propagation')

DEFAULT_TIMEOUT = 2.0


@dataclass(frozen=True)
class PropagationResult:
    """Qué contestó un juez al aviso."""

    endpoint: str
    ok: bool
    status: int = None
    error: str = None


def control_endpoints():
    """Los endpoints de control configurados, en orden."""
    return tuple(getattr(settings, 'DMOJ_JUDGE_CONTROL_ENDPOINTS', None) or ())


def control_timeout():
    return getattr(settings, 'DMOJ_JUDGE_CONTROL_TIMEOUT', DEFAULT_TIMEOUT)


def notify_judge(endpoint, timeout=None):
    """Pide a un juez que reescanee su árbol y devuelve qué contestó.

    Nunca levanta: un endpoint mal formado o un error de red son un resultado
    fallido, porque la publicación que ya está escrita no se revierte.
    """
    timeout = control_timeout() if timeout is None else timeout
    try:
        response = requests.post(endpoint, timeout=timeout, allow_redirects=False)
    except Exception as error:
        return PropagationResult(endpoint, ok=False, error='%s: %s' % (type(error).__name__, error))

    return PropagationResult(endpoint, ok=response.ok, status=response.status_code)


def propagate_problem_change(problem_id=None):
    """Avisa a todos los jueces configurados en paralelo. Nunca levanta.

    Devuelve un resultado por endpoint (en el orden configurado); la vista no
    lo usa, las fallas ya quedaron logueadas. El log se escribe acá y no en
    los workers: la publicación no espera a los que se pasaron del timeout y
    el aviso de timeout ya los cubre.
    """
    endpoints = control_endpoints()
    if not endpoints:
        return ()

    timeout = control_timeout()
    logger.info('problem %s published: asking %d judge(s) to rescan', problem_id, len(endpoints))

    results = {}
    executor = ThreadPoolExecutor(max_workers=len(endpoints), thread_name_prefix='judge-propagate')
    try:
        futures = {executor.submit(notify_judge, endpoint, timeout): endpoint for endpoint in endpoints}
        # La espera se corta en el timeout aunque ``requests`` tarde más: la
        # publicación no puede quedar colgada de un juez que no contesta.
        done, pending = wait(futures, timeout=timeout)
        for future in done:
            endpoint = futures[future]
            result = future.result()
            results[endpoint] = result
            if result.ok:
                logger.info('judge at %s rescans its problems after publishing problem %s',
                            endpoint, problem_id)
            else:
                logger.warning('judge at %s could not be asked to rescan after publishing problem %s: %s',
                               endpoint, problem_id, result.error or 'HTTP %d' % result.status)
        for future in pending:
            endpoint = futures[future]
            logger.warning('judge at %s did not answer within %ss after publishing problem %s',
                           endpoint, timeout, problem_id)
            results[endpoint] = PropagationResult(endpoint, ok=False, error='timeout')
    finally:
        executor.shutdown(wait=False)

    return tuple(results[endpoint] for endpoint in endpoints)
