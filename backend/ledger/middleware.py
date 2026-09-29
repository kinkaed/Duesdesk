"""Security headers and request correlation."""
import logging
import time

from .observability import (
    RequestContext,
    bind_context,
    client_ip,
    new_request_id,
    proxy_trusted,
    reset_context,
    technical,
)

# One access log line per request is the point of correlation, but static assets
# and health probes would drown it. They are skipped, not logged at debug.
UNLOGGED_PREFIXES = ('/static/', '/health/', '/favicon.ico')


def _interesting(path):
    return not path.startswith(UNLOGGED_PREFIXES)


class RequestCorrelationMiddleware:
    """Give every request an id, expose it to logs and the response, and log
    exactly one completion line for it.

    The context is held in a ContextVar rather than a module attribute because
    waitress runs several threads and reuses them: a plain global would hand one
    request's id to an unrelated later request. The reset in the finally block
    is what makes that safe, and it runs on the exception path too.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = new_request_id(request.META)
        request.request_id = request_id
        context = RequestContext(
            request_id=request_id,
            method=request.method,
            path=request.path[:200],
            ip_address=client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:300],
            proxy_trusted=proxy_trusted(),
        )
        token = bind_context(context)
        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            # Log with the full safe context, then let Django raise so the
            # normal 500 machinery still applies and the response stays free of
            # internal detail in production.
            self._enrich(context, request)
            technical('request.failed', 'Request failed', level=logging.ERROR,
                      exc_info=True, **context.as_log_fields(),
                      actor_id=context.actor_id,
                      duration_ms=int((time.monotonic() - started) * 1000))
            reset_context(token)
            raise
        duration = int((time.monotonic() - started) * 1000)
        self._enrich(context, request)
        response['X-Request-Id'] = request_id
        if _interesting(request.path):
            level = logging.WARNING if response.status_code >= 500 else logging.INFO
            technical('request.completed', '%s %s -> %s in %sms',
                      context.method, context.path, response.status_code, duration,
                      level=level, method=context.method, path=context.path,
                      request_id=context.request_id, ip=context.ip_address,
                      status=response.status_code, duration_ms=duration,
                      actor_id=context.actor_id)
        reset_context(token)
        return response

    @staticmethod
    def _enrich(context, request):
        """Add the authenticated user, but only if the view already resolved it.

        request.user is a SimpleLazyObject: reading it opens the session, which
        is a database query on every single request just to decorate a log line.
        AuthenticationMiddleware caches the resolved user in request._cached_user,
        so reading that attribute instead gets the same answer for free on any
        request whose view looked at the user, and does nothing at all otherwise.

        It is read defensively because a logging decorator must not be able to
        turn a working response into a 500: a view that answered without
        touching the session (health, static) must keep answering that way even
        when the database is down.
        """
        try:
            user = getattr(request, '_cached_user', None)
            if user is not None and getattr(user, 'is_authenticated', False):
                context.actor_id = user.pk
        except Exception:
            # Correlation is worth less than the response. Never fail a request
            # over a log field.
            pass


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if not request.path.startswith('/static/'):
            response['Cache-Control'] = 'no-store, private'
        return response
